from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from .autoplay import AutoDecision, AutoPlayState
from .detector import (
    DetectionError,
    geometry_from_rect,
    grid_liveness,
    locate_board,
    read_stones,
    with_screen_origin,
)
from .engine import BLACK, EMPTY, WHITE, MoveAnalysis
from .models import BoardGeometry, ScanResult
from .rapfi import RapfiEngine, RapfiError
from .windows import (
    ClickCancelledError,
    WindowInfo,
    capture_window,
    click_window,
    exclude_window_from_capture,
    key_is_down,
    list_candidate_windows,
)


class GomokuApp:
    GRID_LIVENESS_THRESHOLD = 0.65
    COLORS = {
        "window": "#14171a",
        "surface": "#1d2226",
        "surface_alt": "#252b30",
        "border": "#394047",
        "text": "#f3f4f4",
        "muted": "#a9b0b5",
        "accent": "#e0a84a",
        "accent_active": "#f0bb61",
        "good": "#49b77b",
        "danger": "#e36b63",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.engine = RapfiEngine(think_time_ms=5_000)
        self.auto_state = AutoPlayState(stable_reads=2, click_timeout=4.0)
        self.scan: ScanResult | None = None
        self.suggestion: MoveAnalysis | None = None
        self._target_hwnd: int | None = None
        self._window_choices: dict[str, WindowInfo] = {}
        self._locked_geometry: BoardGeometry | None = None
        self._locked_capture_shape: tuple[int, int] | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._view_transform: tuple[float, float, float, int, int] | None = None
        self._selection_start: tuple[float, float] | None = None
        self._selection_id: int | None = None
        self._selecting = False
        self._pending_image: np.ndarray | None = None
        self._pending_origin = (0, 0)
        self._auto_job: str | None = None
        self._engine_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rapfi-search")
        self._engine_future: Future[MoveAnalysis] | None = None
        self._engine_context: tuple[bytes, object | None, int, str] | None = None
        self._engine_poll_job: str | None = None
        self._scan_failures = 0
        self._last_click_at = 0.0
        self._f8_was_down = False

        self.board_size = tk.IntVar(value=15)
        self.ai_color = tk.StringVar(value="black")
        self.target_window_text = tk.StringVar(value="正在查找棋局窗口...")
        self.confidence_threshold = tk.DoubleVar(value=0.55)
        self.click_allowed = tk.BooleanVar(value=False)
        self.click_twice = tk.BooleanVar(value=True)
        self.auto_enabled = tk.BooleanVar(value=False)
        self.interval_seconds = tk.DoubleVar(value=1.2)
        self.strength_seconds = tk.StringVar(value="5.0")
        self.status_text = tk.StringVar(value="等待识别棋盘")
        self.stats_text = tk.StringVar(value="黑 0  ·  白 0  ·  当前手：未知")
        self.detail_text = tk.StringVar(value="自动点击默认关闭。先识别并核对预览，再开启点击权限。")

        self._configure_window()
        self._configure_styles()
        self._build_ui()
        self.root.after(250, self._activate_capture_exclusion)
        self.root.after(320, self.refresh_windows)
        self.root.after(80, self._poll_emergency_key)

    def _configure_window(self) -> None:
        self.root.title("五子棋下棋助手")
        self.root.geometry("1080x720+40+40")
        self.root.minsize(920, 640)
        self.root.configure(bg=self.COLORS["window"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=self.COLORS["window"])
        style.configure("Panel.TFrame", background=self.COLORS["surface"])
        style.configure(
            "TLabel",
            background=self.COLORS["surface"],
            foreground=self.COLORS["text"],
            font=("Microsoft YaHei UI", 10),
        )
        style.configure("Muted.TLabel", foreground=self.COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=self.COLORS["window"], font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Status.TLabel", background=self.COLORS["window"], foreground=self.COLORS["accent"])
        style.configure("Footer.TLabel", background=self.COLORS["window"], foreground=self.COLORS["text"])
        style.configure(
            "FooterMuted.TLabel",
            background=self.COLORS["window"],
            foreground=self.COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "TButton",
            background=self.COLORS["surface_alt"],
            foreground=self.COLORS["text"],
            bordercolor=self.COLORS["border"],
            focusthickness=2,
            focuscolor=self.COLORS["accent"],
            padding=(12, 8),
            font=("Microsoft YaHei UI", 10),
        )
        style.map("TButton", background=[("active", self.COLORS["border"]), ("pressed", "#30363b")])
        style.configure(
            "Accent.TButton",
            background=self.COLORS["accent"],
            foreground="#17130d",
            bordercolor=self.COLORS["accent"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("Accent.TButton", background=[("active", self.COLORS["accent_active"])])
        style.configure(
            "TCheckbutton",
            background=self.COLORS["surface"],
            foreground=self.COLORS["text"],
            focuscolor=self.COLORS["accent"],
            font=("Microsoft YaHei UI", 10),
        )
        style.map("TCheckbutton", background=[("active", self.COLORS["surface"])])
        style.configure(
            "TRadiobutton",
            background=self.COLORS["surface"],
            foreground=self.COLORS["text"],
            focuscolor=self.COLORS["accent"],
            font=("Microsoft YaHei UI", 10),
        )
        style.map("TRadiobutton", background=[("active", self.COLORS["surface"])])
        style.configure("TSeparator", background=self.COLORS["border"])
        style.configure(
            "TCombobox",
            fieldbackground=self.COLORS["surface_alt"],
            background=self.COLORS["surface_alt"],
            foreground=self.COLORS["text"],
            arrowcolor=self.COLORS["text"],
        )
        style.configure(
            "Horizontal.TScale",
            background=self.COLORS["surface"],
            troughcolor=self.COLORS["surface_alt"],
        )

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame", padding=16)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="五子棋下棋助手", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_text, style="Status.TLabel").grid(row=0, column=1, sticky="e")

        body = ttk.Frame(shell, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        preview_frame = ttk.Frame(body, style="Panel.TFrame", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            preview_frame,
            background="#0e1113",
            highlightthickness=1,
            highlightbackground=self.COLORS["border"],
            cursor="arrow",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.create_text(
            320,
            280,
            text="尚未识别棋盘\n\n点击“识别屏幕”自动定位",
            fill=self.COLORS["muted"],
            justify="center",
            font=("Microsoft YaHei UI", 13),
        )
        self.canvas.bind("<ButtonPress-1>", self._selection_press)
        self.canvas.bind("<B1-Motion>", self._selection_drag)
        self.canvas.bind("<ButtonRelease-1>", self._selection_release)

        controls = ttk.Frame(body, style="Panel.TFrame", padding=16, width=326)
        controls.grid(row=0, column=1, sticky="ns")
        controls.grid_propagate(False)
        controls.columnconfigure(0, weight=1)

        row = 0
        ttk.Label(controls, text="棋盘识别", font=("Microsoft YaHei UI", 11, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        target_row = ttk.Frame(controls, style="Panel.TFrame")
        target_row.grid(row=row, column=0, sticky="ew", pady=(10, 6))
        target_row.columnconfigure(0, weight=1)
        self.window_combo = ttk.Combobox(
            target_row,
            textvariable=self.target_window_text,
            state="readonly",
            width=23,
        )
        self.window_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.window_combo.bind("<<ComboboxSelected>>", self._target_selected)
        ttk.Button(target_row, text="刷新", width=5, command=self.refresh_windows).grid(row=0, column=1)
        row += 1
        action_row = ttk.Frame(controls, style="Panel.TFrame")
        action_row.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        action_row.columnconfigure((0, 1), weight=1)
        ttk.Button(action_row, text="识别窗口", command=lambda: self.scan_screen(force_locate=True)).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(action_row, text="手动框选", command=self.begin_manual_selection).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        row += 1

        size_row = ttk.Frame(controls, style="Panel.TFrame")
        size_row.grid(row=row, column=0, sticky="ew")
        ttk.Label(size_row, text="路数", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(
            size_row, text="15", variable=self.board_size, value=15, command=self._board_size_changed
        ).pack(side="left", padx=(16, 4))
        ttk.Radiobutton(
            size_row, text="19", variable=self.board_size, value=19, command=self._board_size_changed
        ).pack(side="left", padx=4)
        row += 1
        ttk.Separator(controls).grid(row=row, column=0, sticky="ew", pady=9)
        row += 1

        ttk.Label(controls, text="执棋与落点", font=("Microsoft YaHei UI", 11, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        color_row = ttk.Frame(controls, style="Panel.TFrame")
        color_row.grid(row=row, column=0, sticky="ew", pady=(8, 10))
        ttk.Radiobutton(
            color_row, text="我执黑", variable=self.ai_color, value="black", command=self._player_changed
        ).pack(side="left")
        ttk.Radiobutton(
            color_row, text="我执白", variable=self.ai_color, value="white", command=self._player_changed
        ).pack(side="left", padx=(18, 0))
        row += 1
        strength_row = ttk.Frame(controls, style="Panel.TFrame")
        strength_row.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(strength_row, text="Rapfi 思考", style="Muted.TLabel").pack(side="left")
        for label, value in (("快 0.5秒", "0.5"), ("强 2秒", "2.0"), ("最强 5秒", "5.0")):
            ttk.Radiobutton(
                strength_row,
                text=label,
                variable=self.strength_seconds,
                value=value,
                command=self._strength_changed,
            ).pack(side="left", padx=(8 if value == "0.5" else 4, 0))
        row += 1
        ttk.Button(controls, text="计算推荐落点", command=self.calculate_move).grid(row=row, column=0, sticky="ew")
        row += 1
        ttk.Button(controls, text="执行一次落子", style="Accent.TButton", command=self.execute_once).grid(
            row=row, column=0, sticky="ew", pady=(8, 0)
        )
        row += 1
        ttk.Separator(controls).grid(row=row, column=0, sticky="ew", pady=9)
        row += 1

        ttk.Label(controls, text="自动控制", font=("Microsoft YaHei UI", 11, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        ttk.Checkbutton(
            controls,
            text="允许鼠标点击",
            variable=self.click_allowed,
            command=self._click_permission_changed,
        ).grid(row=row, column=0, sticky="w", pady=(8, 2))
        row += 1
        ttk.Checkbutton(
            controls,
            text="同一交点点击两次确认",
            variable=self.click_twice,
        ).grid(row=row, column=0, sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(
            controls,
            text="自动识别并代下",
            variable=self.auto_enabled,
            command=self._auto_changed,
        ).grid(row=row, column=0, sticky="w", pady=2)
        row += 1
        ttk.Label(controls, text="F8：立即关闭自动模式和点击权限", style="Muted.TLabel", wraplength=280).grid(
            row=row, column=0, sticky="w", pady=(4, 8)
        )
        row += 1

        threshold_row = ttk.Frame(controls, style="Panel.TFrame")
        threshold_row.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        threshold_row.columnconfigure(1, weight=1)
        ttk.Label(threshold_row, text="最低置信度", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Scale(
            threshold_row,
            from_=0.42,
            to=0.82,
            variable=self.confidence_threshold,
            orient="horizontal",
        ).grid(row=0, column=1, sticky="ew", padx=(10, 0))
        row += 1
        footer = ttk.Frame(shell, style="App.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(1, weight=1)
        ttk.Label(footer, textvariable=self.stats_text, style="Footer.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            footer,
            textvariable=self.detail_text,
            style="FooterMuted.TLabel",
            justify="right",
            anchor="e",
            wraplength=650,
        ).grid(row=0, column=1, sticky="ew", padx=(24, 0))

    def refresh_windows(self) -> None:
        previous = self._target_hwnd
        try:
            windows = [
                window
                for window in list_candidate_windows()
                if window.title != self.root.title() and window.size[0] >= 280 and window.size[1] >= 280
            ]
        except OSError as exc:
            self._set_status("无法枚举窗口", str(exc))
            return
        choices: dict[str, WindowInfo] = {}
        for window in windows:
            label = window.title
            if label in choices:
                label = f"{label}  [{window.hwnd}]"
            choices[label] = window
        self._window_choices = choices
        self.window_combo.configure(values=list(choices))

        selected_label: str | None = None
        if previous is not None:
            selected_label = next((label for label, item in choices.items() if item.hwnd == previous), None)
        if selected_label is None:
            preferred = [
                label for label, item in choices.items() if "五子棋" in item.title or "gomoku" in item.title.casefold()
            ]
            if preferred:
                selected_label = preferred[0]
        if selected_label is None:
            self._target_hwnd = None
            self.target_window_text.set("请选择棋局窗口")
            self._set_status("请选择目标窗口", "没有自动找到五子棋窗口；请从下拉框中选择棋局。")
            return
        self.target_window_text.set(selected_label)
        selected = choices[selected_label]
        if selected.hwnd != previous:
            self._bind_window(selected)

    def _target_selected(self, _event: tk.Event | None = None) -> None:
        selected = self._window_choices.get(self.target_window_text.get())
        if selected is not None:
            self._bind_window(selected)

    def _bind_window(self, window: WindowInfo) -> None:
        self._stop_auto("目标窗口已切换")
        self._target_hwnd = window.hwnd
        self._locked_geometry = None
        self._locked_capture_shape = None
        self.scan = None
        self.suggestion = None
        self.auto_state.reset()
        self._set_status("已绑定棋局窗口", window.title)
        self.root.after(80, lambda: self.scan_screen(force_locate=True))

    def _board_size_changed(self) -> None:
        self._stop_auto("棋盘路数已更改")
        self._locked_geometry = None
        self._locked_capture_shape = None
        self.scan = None
        self.auto_state.reset()

    def _player_changed(self) -> None:
        self._stop_auto("执棋颜色已更改")
        self.auto_state.reset()

    def _strength_changed(self) -> None:
        self._stop_auto("Rapfi 棋力档位已更改")
        self.engine.set_think_time_ms(round(float(self.strength_seconds.get()) * 1000))

    @staticmethod
    def _board_signature(board: np.ndarray) -> bytes:
        return np.asarray(board, dtype=np.int8).tobytes()

    def _capture_target(self) -> tuple[np.ndarray, tuple[int, int]]:
        if self._target_hwnd is None:
            self.refresh_windows()
        if self._target_hwnd is None:
            raise OSError("尚未选择棋局窗口")
        return capture_window(self._target_hwnd)

    def _activate_capture_exclusion(self) -> None:
        self.root.update_idletasks()
        if not exclude_window_from_capture(self.root.winfo_id()):
            self.detail_text.set("系统不支持隐藏控制台截图；请将控制台移到棋盘外侧。")

    def _set_status(self, text: str, detail: str | None = None) -> None:
        self.status_text.set(text)
        if detail is not None:
            self.detail_text.set(detail)
        self.root.update_idletasks()

    def _player(self) -> int:
        return BLACK if self.ai_color.get() == "black" else WHITE

    def _geometry_key(self) -> tuple[object, ...] | None:
        if self.scan is None:
            return None
        geometry = self.scan.geometry
        return (
            self._target_hwnd,
            self.scan.image_rgb.shape[:2],
            tuple(np.rint(geometry.x_lines).astype(int)),
            tuple(np.rint(geometry.y_lines).astype(int)),
        )

    def _recognition_quality(self) -> float:
        if self.scan is None:
            return 0.0
        return float(
            min(
                self.scan.geometry.confidence,
                self.scan.grid_liveness,
                np.min(self.scan.stone_confidence),
            )
        )

    @staticmethod
    def _turn(board: np.ndarray) -> int | None:
        return AutoPlayState.turn(board)

    def _update_stats(self) -> None:
        if self.scan is None:
            self.stats_text.set("黑 0  ·  白 0  ·  当前手：未知")
            return
        board = self.scan.board
        black = int(np.count_nonzero(board == BLACK))
        white = int(np.count_nonzero(board == WHITE))
        turn = self._turn(board)
        turn_text = "黑" if turn == BLACK else "白" if turn == WHITE else "计数异常"
        self.stats_text.set(f"黑 {black}  ·  白 {white}  ·  当前手：{turn_text}")

    def scan_screen(self, quiet: bool = False, force_locate: bool = False) -> bool:
        try:
            image_rgb, origin = self._capture_target()
            capture_shape = image_rgb.shape[:2]
            if force_locate or self._locked_capture_shape != capture_shape:
                self._locked_geometry = None
                self._locked_capture_shape = None
            if self._locked_geometry is None:
                base_geometry = locate_board(image_rgb, self.board_size.get())
                self._locked_geometry = with_screen_origin(base_geometry, (0, 0))
                self._locked_capture_shape = capture_shape
            geometry = with_screen_origin(self._locked_geometry, origin)
            liveness = grid_liveness(image_rgb, geometry)
            board, stone_confidence = read_stones(image_rgb, geometry)
            self.scan = ScanResult(image_rgb, geometry, board, stone_confidence, liveness)
            self.suggestion = None
            self._show_scan()
            self._update_stats()
            if liveness < self.GRID_LIVENESS_THRESHOLD:
                self._set_status(
                    "棋盘网格已失效",
                    f"当前锁定网格存活度 {liveness:.0%}，可能已切换页面、被遮挡或发生平移；为避免误点，已拒绝本次操作。",
                )
                return False
            quality = self._recognition_quality()
            if quality < self.confidence_threshold.get():
                self._set_status(
                    "识别置信度不足",
                    f"当前最低识别置信度 {quality:.0%}，低于点击门槛 {self.confidence_threshold.get():.0%}。请重新识别或手动框选。",
                )
                return False
            if not quiet:
                self._set_status(
                    "棋盘已识别",
                    f"已锁定目标窗口网格；定位 {geometry.confidence:.0%}，最低交点 {np.min(stone_confidence):.0%}。",
                )
            return True
        except (DetectionError, OSError, ValueError) as exc:
            self._set_status("自动识别失败" if quiet else "识别失败", str(exc))
            return False

    def calculate_move(self, quiet: bool = False) -> None:
        if not self.scan_screen(quiet=quiet):
            return
        self._start_engine_search("suggest")

    def _current_search_board(self) -> tuple[np.ndarray, int] | None:
        assert self.scan is not None
        if self._recognition_quality() < self.confidence_threshold.get():
            self._set_status("暂不计算", "棋盘定位置信度未达到门槛，请先校准。")
            return None
        if self.engine.winner(self.scan.board) != EMPTY:
            self._set_status("棋局已结束", "检测到棋盘上已经有五连。")
            return None
        turn = self._turn(self.scan.board)
        if turn is None:
            self._set_status("棋子计数异常", "黑白棋子数量不符合轮次，禁止自动点击；请核对预览或重新框选。")
            return None
        player = self._player()
        if turn != player:
            self._set_status("等待对方落子", "当前不是你方回合，未计算和点击。")
            return None
        return self.scan.board.copy(), player

    def execute_once(self) -> bool:
        if not self.click_allowed.get():
            self.root.bell()
            self._set_status("点击权限未开启", "先核对识别预览，再勾选“允许鼠标点击”。")
            return False
        scanned = False
        if self.auto_state.pending_move is not None:
            if not self.scan_screen():
                return False
            assert self.scan is not None
            scanned = True
            observation = self.auto_state.observe(
                self.scan.board,
                self._player(),
                time.monotonic(),
                self._geometry_key(),
            )
            if observation.decision == AutoDecision.ERROR:
                self._set_status("上一步确认异常", observation.message)
                return False
            if observation.decision != AutoDecision.CLICK_CONFIRMED:
                self._set_status("等待上一步确认", observation.message)
                return False
        if self.auto_state.blocked_reason is not None:
            self.auto_state.reset()
        if not scanned and not self.scan_screen():
            return False
        return self._start_engine_search("execute")

    def _click_current_move(self, move: MoveAnalysis) -> bool:
        if not self.click_allowed.get():
            self._set_status("点击权限未开启", "自动点击已被拦截。")
            return False
        if self.scan is None or self._target_hwnd is None:
            self._set_status("无法点击", "没有当前棋盘帧或目标窗口。")
            return False
        if self.auto_state.pending_move is not None:
            self._set_status("等待上一步确认", "不会在旧盘面上重复点击。")
            return False
        if self.scan.board[move.row, move.col] != EMPTY:
            self._set_status("目标格非空", "识别结果已变化，取消本次点击。")
            return False
        if float(self.scan.stone_confidence[move.row, move.col]) < self.confidence_threshold.get():
            self._set_status("目标格识别不可靠", "目标交点置信度不足，取消本次点击。")
            return False
        if time.monotonic() - self._last_click_at < 0.8:
            self._set_status("点击冷却中", "为防止重复落子，本次请求已拦截。")
            return False
        x, y = self.scan.geometry.screen_point(move.row, move.col)
        try:
            click_window(
                self._target_hwnd,
                x,
                y,
                clicks=2 if self.click_twice.get() else 1,
                cancelled=lambda: key_is_down(0x77),
            )
        except ClickCancelledError as exc:
            self._stop_auto(str(exc), revoke_click=True)
            return False
        except OSError as exc:
            self._set_status("点击失败", str(exc))
            return False
        self._last_click_at = time.monotonic()
        self.auto_state.mark_clicked(
            self.scan.board,
            (move.row, move.col),
            self._player(),
            self._last_click_at,
            self._geometry_key(),
        )
        self._set_status(
            f"已点击 {chr(65 + move.col)}{move.row + 1}",
            f"已在目标窗口坐标 ({x}, {y}) 落子；等待目标格视觉确认后才会继续。",
        )
        return True

    def begin_manual_selection(self) -> None:
        self._stop_auto("正在手动框选棋盘")
        self.auto_state.reset()
        try:
            self._pending_image, self._pending_origin = self._capture_target()
        except OSError as exc:
            self._set_status("截图失败", str(exc))
            return
        self._selecting = True
        self.scan = None
        self.suggestion = None
        self.canvas.configure(cursor="crosshair")
        self._display_image(self._pending_image, full=True)
        self._set_status("拖框选择棋盘", "在左侧截图上从棋盘左上角拖到右下角；范围应包含完整网格。")

    def _selection_press(self, event: tk.Event) -> None:
        if not self._selecting:
            return
        self._selection_start = (event.x, event.y)
        if self._selection_id is not None:
            self.canvas.delete(self._selection_id)
        self._selection_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline=self.COLORS["accent"],
            width=2,
        )

    def _selection_drag(self, event: tk.Event) -> None:
        if not self._selecting or self._selection_start is None or self._selection_id is None:
            return
        self.canvas.coords(self._selection_id, *self._selection_start, event.x, event.y)

    def _canvas_to_image(self, x: float, y: float) -> tuple[int, int]:
        if self._view_transform is None:
            return 0, 0
        scale, offset_x, offset_y, source_x, source_y = self._view_transform
        return int(round((x - offset_x) / scale + source_x)), int(round((y - offset_y) / scale + source_y))

    def _selection_release(self, event: tk.Event) -> None:
        if not self._selecting or self._selection_start is None or self._pending_image is None:
            return
        start = self._canvas_to_image(*self._selection_start)
        end = self._canvas_to_image(event.x, event.y)
        try:
            geometry = geometry_from_rect(self._pending_image, (*start, *end), self.board_size.get())
            self._locked_geometry = with_screen_origin(geometry, (0, 0))
            self._locked_capture_shape = self._pending_image.shape[:2]
            screen_geometry = with_screen_origin(self._locked_geometry, self._pending_origin)
            liveness = grid_liveness(self._pending_image, screen_geometry)
            board, stone_confidence = read_stones(self._pending_image, screen_geometry)
            self.scan = ScanResult(
                self._pending_image,
                screen_geometry,
                board,
                stone_confidence,
                liveness,
            )
            self.suggestion = None
            self._selecting = False
            self.canvas.configure(cursor="arrow")
            self._show_scan()
            self._update_stats()
            if liveness < self.GRID_LIVENESS_THRESHOLD:
                self._set_status(
                    "手动框选需重新确认",
                    f"定位置信度 {geometry.confidence:.0%}，但当前网格存活度仅 {liveness:.0%}；请框住完整网格。",
                )
            else:
                self._set_status("手动校准完成", f"网格置信度 {geometry.confidence:.0%}。请核对棋子预览。")
        except (DetectionError, ValueError) as exc:
            self._set_status("框选无效", str(exc))
        finally:
            self._selection_start = None
            self._selection_id = None

    def _annotated_image(self) -> np.ndarray:
        assert self.scan is not None
        image = self.scan.image_rgb.copy()
        geometry = self.scan.geometry
        left, top, right, bottom = geometry.bounds
        cv2.rectangle(image, (left, top), (right, bottom), (73, 183, 123), 3)
        radius = max(3, int(round(geometry.spacing * 0.18)))
        for row in range(geometry.size):
            for col in range(geometry.size):
                value = int(self.scan.board[row, col])
                if value == EMPTY:
                    continue
                x, y = int(round(geometry.x_lines[col])), int(round(geometry.y_lines[row]))
                color = (28, 31, 34) if value == BLACK else (246, 246, 242)
                outline = (235, 176, 77) if value == BLACK else (56, 61, 65)
                cv2.circle(image, (x, y), radius, color, -1)
                cv2.circle(image, (x, y), radius, outline, 2)
        if self.suggestion is not None:
            x = int(round(geometry.x_lines[self.suggestion.col]))
            y = int(round(geometry.y_lines[self.suggestion.row]))
            target_radius = max(8, int(round(geometry.spacing * 0.31)))
            cv2.circle(image, (x, y), target_radius, (73, 183, 123), 3)
            cv2.line(image, (x - target_radius, y), (x + target_radius, y), (73, 183, 123), 2)
            cv2.line(image, (x, y - target_radius), (x, y + target_radius), (73, 183, 123), 2)
        return image

    def _show_scan(self) -> None:
        if self.scan is not None:
            self._display_image(self._annotated_image(), geometry=self.scan.geometry)

    def _display_image(self, image_rgb: np.ndarray, geometry: BoardGeometry | None = None, full: bool = False) -> None:
        self.root.update_idletasks()
        canvas_width = max(200, self.canvas.winfo_width())
        canvas_height = max(200, self.canvas.winfo_height())
        if geometry is not None and not full:
            left, top, right, bottom = geometry.bounds
            pad = int(round(geometry.spacing * 1.2))
            source_x = max(0, left - pad)
            source_y = max(0, top - pad)
            source_right = min(image_rgb.shape[1], right + pad + 1)
            source_bottom = min(image_rgb.shape[0], bottom + pad + 1)
        else:
            source_x = source_y = 0
            source_right, source_bottom = image_rgb.shape[1], image_rgb.shape[0]
        crop = image_rgb[source_y:source_bottom, source_x:source_right]
        scale = min(canvas_width / crop.shape[1], canvas_height / crop.shape[0])
        display_width = max(1, int(round(crop.shape[1] * scale)))
        display_height = max(1, int(round(crop.shape[0] * scale)))
        resized = Image.fromarray(crop).resize((display_width, display_height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        offset_x = (canvas_width - display_width) / 2
        offset_y = (canvas_height - display_height) / 2
        self.canvas.delete("all")
        self.canvas.create_image(offset_x, offset_y, anchor="nw", image=self._photo)
        self._view_transform = (scale, offset_x, offset_y, source_x, source_y)

    def _click_permission_changed(self) -> None:
        if not self.click_allowed.get():
            self._stop_auto("鼠标点击权限已关闭")
        state = "已开启" if self.click_allowed.get() else "已关闭"
        self._set_status(f"鼠标点击{state}")

    def _auto_changed(self) -> None:
        if self.auto_enabled.get() and not self.click_allowed.get():
            self.auto_enabled.set(False)
            self.root.bell()
            self._set_status("无法开启自动模式", "必须先开启“允许鼠标点击”。")
            return
        if self.auto_enabled.get():
            if self._target_hwnd is None:
                self.auto_enabled.set(False)
                self._set_status("无法开启自动模式", "请先选择棋局窗口。")
                return
            self.auto_state.reset()
            self._scan_failures = 0
            self._set_status("自动模式已启动", "仅在识别可靠且轮到你方时点击；按 F8 可立即停止。")
            self._schedule_auto(80)
        else:
            self.auto_state.reset()
            self._set_status("自动模式已停止")

    def _stop_auto(self, reason: str, *, reset_state: bool = True, revoke_click: bool = False) -> None:
        self.auto_enabled.set(False)
        if revoke_click:
            self.click_allowed.set(False)
        if self._auto_job is not None:
            try:
                self.root.after_cancel(self._auto_job)
            except tk.TclError:
                pass
            self._auto_job = None
        self._engine_context = None
        if reset_state:
            self.auto_state.reset()
        self._scan_failures = 0
        self._set_status("自动模式已停止", reason)

    def _schedule_auto(self, delay_ms: int | None = None) -> None:
        if self._auto_job is not None:
            self.root.after_cancel(self._auto_job)
        delay = delay_ms if delay_ms is not None else int(self.interval_seconds.get() * 1000)
        self._auto_job = self.root.after(max(80, delay), self._auto_tick)

    def _start_engine_search(self, action: str) -> bool:
        if action not in ("suggest", "execute", "auto"):
            raise ValueError(f"未知引擎操作：{action}")
        if self.scan is None:
            return False
        if self._engine_future is not None:
            self._set_status("Rapfi 正在思考", "已有一个棋盘正在计算，请等待当前结果。")
            return False
        current = self._current_search_board()
        if current is None:
            return False
        board, player = current
        self._engine_context = (self._board_signature(board), self._geometry_key(), player, action)
        self._engine_future = self._engine_executor.submit(self.engine.analyze, board, player)
        self._set_status(
            "Rapfi 正在思考",
            f"强引擎最多思考 {float(self.strength_seconds.get()):g} 秒；期间 F8 仍可立即禁止点击。",
        )
        self._schedule_engine_poll()
        return True

    def _start_auto_search(self) -> None:
        self._start_engine_search("auto")

    def _schedule_engine_poll(self) -> None:
        if self._engine_poll_job is not None:
            try:
                self.root.after_cancel(self._engine_poll_job)
            except tk.TclError:
                pass
        self._engine_poll_job = self.root.after(80, self._poll_engine_result)

    def _resume_auto_after_engine(self, delay_ms: int = 80) -> None:
        if self.auto_enabled.get() and self._auto_job is None:
            self._schedule_auto(delay_ms)

    def _poll_engine_result(self) -> None:
        self._engine_poll_job = None
        future = self._engine_future
        if future is None:
            return
        if not future.done():
            self._schedule_engine_poll()
            return
        self._engine_future = None
        context = self._engine_context
        self._engine_context = None
        try:
            move = future.result()
        except (ValueError, RapfiError) as exc:
            action = context[3] if context is not None else None
            if action == "auto" and self.auto_enabled.get():
                self._stop_auto(f"强引擎计算失败：{exc}", reset_state=False)
            elif action is not None:
                self._set_status("强引擎计算失败", f"{exc}。为避免弱棋或误点，本次不会使用旧引擎代替。")
                self._resume_auto_after_engine()
            return
        except Exception as exc:
            action = context[3] if context is not None else None
            if action == "auto" and self.auto_enabled.get():
                self._stop_auto(f"强引擎线程异常：{exc}", reset_state=False)
            elif action is not None:
                self._set_status("强引擎线程异常", str(exc))
                self._resume_auto_after_engine()
            return

        if context is None:
            self._resume_auto_after_engine()
            return
        signature, geometry_key, player, action = context
        if action == "auto" and (not self.auto_enabled.get() or not self.click_allowed.get()):
            return
        if action == "execute" and not self.click_allowed.get():
            self._set_status("点击权限已关闭", "Rapfi 思考完成，但点击授权已撤销，未执行落子。")
            self._resume_auto_after_engine()
            return
        if player != self._player():
            if action == "auto":
                self._stop_auto("思考期间执棋颜色发生变化。")
            else:
                self._set_status("已丢弃旧落点", "思考期间执棋颜色发生变化。")
                self._resume_auto_after_engine()
            return
        if not self.scan_screen(quiet=True) or self.scan is None:
            if action == "auto":
                self._stop_auto("Rapfi 思考完成后无法重新确认棋盘。")
            else:
                self._set_status("已丢弃旧落点", "Rapfi 思考完成后无法重新确认棋盘。")
                self._resume_auto_after_engine()
            return
        if self._geometry_key() != geometry_key or self._board_signature(self.scan.board) != signature:
            if action == "auto":
                self.auto_state.reset()
                self._set_status("棋盘已变化", "Rapfi 思考期间盘面或网格发生变化；旧落点已丢弃，将重新稳定识别。")
                self._schedule_auto(80)
            else:
                self._set_status("已丢弃旧落点", "Rapfi 思考期间盘面或网格发生变化，请重新计算。")
                self._resume_auto_after_engine()
            return
        self.suggestion = move
        self._show_scan()
        if action == "suggest":
            self._set_status(
                f"推荐 {chr(65 + move.col)}{move.row + 1}",
                f"行 {move.row + 1}，列 {move.col + 1}；原因：{move.reason}。绿色标记处为目标落点。",
            )
            self._resume_auto_after_engine()
            return
        if not self._click_current_move(move):
            if action == "auto":
                self._stop_auto("强引擎落点未能安全点击。", reset_state=False)
            else:
                self._resume_auto_after_engine()
            return
        if action == "auto":
            self._resume_auto_after_engine(int(self.interval_seconds.get() * 1000))
        else:
            self._resume_auto_after_engine()

    def _auto_tick(self) -> None:
        self._auto_job = None
        if not self.auto_enabled.get():
            return
        if not self.click_allowed.get():
            self._stop_auto("点击权限已关闭。")
            return
        try:
            valid = self.scan_screen(quiet=True)
            if not valid or self.scan is None:
                self._scan_failures += 1
                if self._scan_failures >= 3:
                    self._stop_auto("连续三次无法可靠识别棋盘。请检查目标窗口和预览。")
                    return
                self._set_status("等待重新识别", f"识别暂时失败 {self._scan_failures}/3，未执行点击。")
                return
            self._scan_failures = 0
            observation = self.auto_state.observe(
                self.scan.board,
                self._player(),
                time.monotonic(),
                self._geometry_key(),
            )
            if observation.decision == AutoDecision.ERROR:
                self._stop_auto(observation.message, reset_state=False)
                return
            if observation.decision == AutoDecision.READY:
                self._start_auto_search()
                return
            if observation.decision == AutoDecision.CLICK_CONFIRMED:
                self._set_status("落子已确认", observation.message)
            elif observation.decision == AutoDecision.CLICK_PENDING:
                self._set_status("等待落子确认", observation.message)
            elif observation.decision == AutoDecision.WAIT_TURN:
                self._set_status("等待对方落子", observation.message)
            else:
                self._set_status("棋盘稳定检查", observation.message)
        except Exception as exc:
            self._stop_auto(f"自动循环异常：{exc}", reset_state=False)
        finally:
            if self.auto_enabled.get() and self._auto_job is None and self._engine_future is None:
                self._schedule_auto()

    def _poll_emergency_key(self) -> None:
        down = key_is_down(0x77)  # F8
        if down and not self._f8_was_down:
            self._stop_auto("F8 急停已触发；自动模式和鼠标点击权限均已关闭。", revoke_click=True)
        self._f8_was_down = down
        self.root.after(80, self._poll_emergency_key)

    def close(self) -> None:
        self._stop_auto("窗口已关闭", revoke_click=True)
        if self._engine_poll_job is not None:
            try:
                self.root.after_cancel(self._engine_poll_job)
            except tk.TclError:
                pass
            self._engine_poll_job = None
        if self._engine_future is not None:
            self._engine_future.cancel()
        self.engine.abort()
        self.root.destroy()
        try:
            self._engine_executor.submit(self.engine.close)
        except RuntimeError:
            pass
        self._engine_executor.shutdown(wait=False, cancel_futures=False)
