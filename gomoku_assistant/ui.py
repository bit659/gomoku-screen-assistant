from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from .autoplay import AutoDecision, AutoPlayState
from .detector import (
    DetectionError,
    find_confirm_button,
    geometry_from_rect,
    grid_liveness,
    locate_board,
    read_stones,
    with_screen_origin,
)
from .engine import BLACK, EMPTY, WHITE, MoveAnalysis
from .models import BoardGeometry, ScanResult
from .rapfi import RapfiEngine, RapfiError
from .template_matcher import (
    TemplateLibrary,
    load_template_package,
    make_square_box,
    read_board_template,
)
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
        self.auto_state = AutoPlayState(stable_reads=2, click_timeout=6.0)
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
        self._pending_engine_result: tuple[MoveAnalysis, tuple[bytes, object | None, int, str]] | None = None
        self._engine_poll_job: str | None = None
        self._scan_failures = 0
        self._auto_suspended = False  # 手动框选棋盘期间保持自动模式开启但暂停采样点击
        self._last_click_at = 0.0
        self._f8_was_down = False
        self._f9_was_down = False
        self._f10_was_down = False
        # 确认按钮定位方式：auto=黄色掩膜自动识别；manual=左侧视窗手动点选坐标。
        self.confirm_source = tk.StringVar(value="auto")
        self.manual_confirm_xy: tuple[int, int] | None = None
        self._confirm_point_mode = False
        self._confirm_dot_id: int | None = None
        self._confirm_dot_r = 5  # 橙色圆点视窗像素半径（比棋子小一号）
        self._confirm_restore_auto = False  # 进入选择模式前自动模式是否开启（退出/锁定时恢复）
        # 执棋颜色自动识别：勾选后由软件接管我执黑/我执白并停用 F9；手动定位执子图标。
        self.side_auto = tk.BooleanVar(value=False)
        self._stone_point_mode = False  # 执子图标定位模式
        self._stone_dot_id: int | None = None
        self._stone_rect: tuple[int, int, int, int, str] | None = None  # (x0,y0,x1,y1,color) 图像坐标
        self._stone_restore_auto = False  # 进入执子图标定位前自动模式是否开启（锁定时恢复）

        self.board_size = tk.IntVar(value=15)
        self.ai_color = tk.StringVar(value="black")
        self.target_window_text = tk.StringVar(value="正在查找棋局窗口...")
        self.confidence_threshold = tk.DoubleVar(value=0.55)
        # 识别方式：template=OpenCV 多模板匹配（默认主识别）/ color=颜色阈值分割（保留原路径）。
        self.recognition_mode = tk.StringVar(value="color")
        self.template_lib: TemplateLibrary | None = None
        self.template_folder_text = tk.StringVar(value="未导入")
        self.template_status_text = tk.StringVar(value="模板参数：")
        # 点击式拉框状态（第一部分机制移植）：两击定正方形 + 贯穿十字线 + 方向键微调 + 回车选角。
        self._selection_anchor: tuple[float, float] | None = None
        self._selection_square: tuple[int, int, int, int] | None = None
        self._selection_cursor: tuple[float, float] | None = None
        self._xhair_h: int | None = None
        self._xhair_v: int | None = None
        self.click_allowed = tk.BooleanVar(value=True)  # 允许鼠标点击：软件打开默认开启
        self.click_mode = tk.StringVar(value="single_confirm")
        self.auto_enabled = tk.BooleanVar(value=False)
        self.interval_seconds = tk.DoubleVar(value=0.5)  # 自动模式采样间隔：一秒两次
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
        # 竖版主窗口：左侧预览视窗接近小程序窗口比例（≈1.8×宽），
        # 完整显示窗口截图（含棋盘上下与确认按钮），不裁剪。
        # 右侧功能区宽度 = 435 * 4/5 = 348，窗口总宽 ≈ 510 + 12 + 348 + 32 ≈ 902。
        self.root.geometry("902x990+40+40")
        self.root.minsize(902, 860)
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
        # 紧凑小按钮（定位/复位）：长宽约为默认按钮的 3/4。
        style.configure("Compact.TButton", padding=(7, 4))
        # 自动识别并代下开关按钮：关闭=灰，开启=亮灰（接近白）。
        style.configure(
            "AutoOff.TButton",
            background="#909090",
            foreground="#FFFFFF",
            padding=(12, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("AutoOff.TButton", background=[("active", "#828282")])
        style.configure(
            "AutoOn.TButton",
            background="#E9E9E9",
            foreground="#2E2E2E",
            padding=(12, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map("AutoOn.TButton", background=[("active", "#DFDFDF")])
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

        # 左右分隔条：预览视窗 | 功能区，鼠标按住可拖动；当前左右宽度即最小可压缩宽度。
        paned = tk.PanedWindow(
            body,
            orient="horizontal",
            sashwidth=12,
            sashrelief="flat",
            bg=self.COLORS["window"],
        )
        paned.grid(row=0, column=0, sticky="nsew")

        preview_frame = ttk.Frame(paned, style="Panel.TFrame", padding=10)
        paned.add(preview_frame, minsize=510)
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
            300,
            480,
            text="尚未识别棋盘\n\n点击“识别屏幕”自动定位",
            fill=self.COLORS["muted"],
            justify="center",
            font=("Microsoft YaHei UI", 13),
        )
        self.canvas.bind("<ButtonPress-1>", self._selection_press)
        self.canvas.bind("<B1-Motion>", self._selection_drag)
        self.canvas.bind("<ButtonRelease-1>", self._selection_release)
        self.canvas.bind("<Motion>", self._confirm_motion)
        # 点击式拉框：方向键微调逻辑光标、回车等效左键（与第一部分机制一致）。
        self.canvas.bind("<KeyPress-Left>", self._selection_key)
        self.canvas.bind("<KeyPress-Right>", self._selection_key)
        self.canvas.bind("<KeyPress-Up>", self._selection_key)
        self.canvas.bind("<KeyPress-Down>", self._selection_key)
        self.canvas.bind("<KeyPress-Return>", self._selection_return)

        controls = ttk.Frame(paned, style="Panel.TFrame", padding=16, width=348)
        paned.add(controls, minsize=348)
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

        ttk.Label(controls, text="图像识别", font=("Microsoft YaHei UI", 11, "bold")).grid(row=row, column=0, sticky="w")
        row += 1
        recog_row = ttk.Frame(controls, style="Panel.TFrame")
        recog_row.grid(row=row, column=0, sticky="ew", pady=(8, 6))
        ttk.Label(recog_row, text="识别方式", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(
            recog_row,
            text="模板匹配",
            variable=self.recognition_mode,
            value="template",
            command=self._recognition_mode_changed,
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            recog_row,
            text="颜色阈值",
            variable=self.recognition_mode,
            value="color",
            command=self._recognition_mode_changed,
        ).pack(side="left", padx=(8, 0))
        row += 1
        import_row = ttk.Frame(controls, style="Panel.TFrame")
        import_row.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        import_row.columnconfigure(1, weight=1)
        ttk.Button(import_row, text="导入模板文件夹", command=self._import_template_package).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(import_row, textvariable=self.template_folder_text, style="Muted.TLabel", wraplength=210).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )
        row += 1
        ttk.Label(
            controls,
            textvariable=self.template_status_text,
            style="Muted.TLabel",
            wraplength=300,
        ).grid(row=row, column=0, sticky="w", pady=(0, 4))
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
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            color_row,
            text="识别执棋颜色",
            variable=self.side_auto,
            command=self._side_auto_changed,
        ).pack(side="left", padx=(4, 0))
        ttk.Button(color_row, text="定位", style="Compact.TButton", width=4, command=self._start_stone_point_selection).pack(
            side="left", padx=(4, 0)
        )
        row += 1
        confirm_row = ttk.Frame(controls, style="Panel.TFrame")
        confirm_row.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(confirm_row, text="确认按钮", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(
            confirm_row,
            text="自动识别",
            variable=self.confirm_source,
            value="auto",
            command=self._confirm_source_changed,
        ).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(
            confirm_row,
            text="手动定位",
            variable=self.confirm_source,
            value="manual",
            command=self._confirm_source_changed,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(confirm_row, text="定位", style="Compact.TButton", width=4, command=self._start_confirm_point_selection).pack(
            side="left", padx=(8, 0)
        )
        row += 1
        mode_box = ttk.Frame(controls, style="Panel.TFrame")
        mode_box.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        mode_box.columnconfigure(1, weight=1)
        ttk.Label(mode_box, text="点击模式", style="Muted.TLabel").grid(
            row=0, column=0, sticky="nw", pady=(4, 0)
        )
        mode_opts = ttk.Frame(mode_box, style="Panel.TFrame")
        mode_opts.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Radiobutton(
            mode_opts, text="单击", variable=self.click_mode, value="single"
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_opts, text="同一交点双击", variable=self.click_mode, value="double"
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_opts, text="单击+确认按钮", variable=self.click_mode, value="single_confirm"
        ).pack(anchor="w")
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
        calc_row = ttk.Frame(controls, style="Panel.TFrame")
        calc_row.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        calc_row.columnconfigure((0, 1), weight=1)
        ttk.Button(calc_row, text="计算推荐落点", command=self.calculate_move).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(calc_row, text="执行一次落子", style="Accent.TButton", command=self.execute_once).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )
        row += 1
        self.auto_toggle_btn = ttk.Button(
            controls,
            text="自动识别并代下",
            style="AutoOff.TButton",
            command=self._toggle_auto_mode,
        )
        self.auto_toggle_btn.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        row += 1
        click_row = ttk.Frame(controls, style="Panel.TFrame")
        click_row.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(
            click_row,
            text="允许鼠标点击",
            variable=self.click_allowed,
            command=self._click_permission_changed,
        ).pack(side="left")
        row += 1
        for tip in (
            "F8：立即关闭自动模式和点击权限",
            "F9：切换我执黑/我执白",
            "F10：开启/关闭自动模式",
        ):
            ttk.Label(controls, text=tip, style="Muted.TLabel", wraplength=280).grid(
                row=row, column=0, sticky="w", pady=(0, 2)
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
        ttk.Button(
            threshold_row,
            text="复位",
            style="Compact.TButton",
            width=4,
            command=lambda: (
                self.confidence_threshold.set(0.55),
                self._set_status("置信度已复位", "最低置信度已恢复默认 0.55。"),
            ),
        ).grid(row=0, column=2, padx=(6, 0))
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
        # 切换目标窗口不再停用自动模式：若自动模式开着，重置状态后保持开启，
        # 下一轮采样会对新窗口重新识别定位。
        if self.auto_enabled.get():
            self.auto_state.reset()
            self._engine_context = None
            self._pending_engine_result = None
            self._scan_failures = 0
        self._target_hwnd = window.hwnd
        self._locked_geometry = None
        self._locked_capture_shape = None
        self.scan = None
        self.suggestion = None
        self.auto_state.reset()
        self._set_status("已绑定棋局窗口", window.title)
        self.root.after(80, lambda: self.scan_screen(force_locate=True))

    def _board_size_changed(self) -> None:
        # 路数变更不再停用自动模式：重置状态后保持开启，重新定位识别。
        if self.auto_enabled.get():
            self.auto_state.reset()
            self._engine_context = None
            self._pending_engine_result = None
            self._scan_failures = 0
        self._locked_geometry = None
        self._locked_capture_shape = None
        self.scan = None
        self.auto_state.reset()

    def _player_changed(self) -> None:
        # 执棋颜色由软件自动识别控制时：拦截与识别结果不符的手动点击，还原并提示取消勾选。
        if self.side_auto.get():
            detected = self._stone_rect[4] if self._stone_rect is not None else None
            if detected is not None and self.ai_color.get() != detected:
                self.ai_color.set(detected)  # 还原为软件控制值（值相符后走正常逻辑）
                self.root.bell()
                self._set_status("执棋颜色由软件控制", "请先取消勾选“识别执棋颜色”，再手动选择我执黑/我执白。")
                return
        # 执棋颜色变更不再停用自动模式：重置状态后保持开启，下一轮按新颜色继续。
        if self.auto_enabled.get():
            self.auto_state.reset()
            self._engine_context = None
            self._pending_engine_result = None
        self.auto_state.reset()

    def _strength_changed(self) -> None:
        # Rapfi 思考档位变更不影响盘面，不重置状态、不停用自动模式。
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

    def _board_quality(self) -> float:
        """棋盘识别质量：棋盘定位置信度与网格存活度的较小值（滑块原值门槛）。"""
        if self.scan is None:
            return 0.0
        return float(min(self.scan.geometry.confidence, self.scan.grid_liveness))

    def _stone_threshold(self) -> float:
        """棋子置信度门槛 = 滑块值 × 0.6，小数位数与滑块值一致。

        棋盘定位 / 网格存活度仍按滑块原值门槛，仅棋子识别门槛放宽到 60%，
        用于容忍复杂满子局面下个别交点判子偏弱的情况。
        """
        slider = self.confidence_threshold.get()
        text = str(slider)
        decimals = len(text.split(".")[1]) if "." in text else 0
        return round(slider * 0.6, decimals)

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
            board, stone_confidence = self._read_board(image_rgb, geometry)
            self.scan = ScanResult(image_rgb, geometry, board, stone_confidence, liveness)
            self.suggestion = None
            self._show_scan()
            self._update_stats()
            self._refresh_stone_color()  # 持续识别执棋颜色（颜色变化自动更新并重画方框）
            if liveness < self.GRID_LIVENESS_THRESHOLD:
                self._set_status(
                    "棋盘网格已失效",
                    f"当前锁定网格存活度 {liveness:.0%}，可能已切换页面、被遮挡或发生平移；为避免误点，已拒绝本次操作。",
                )
                return False
            board_quality = min(geometry.confidence, liveness)
            stone_min = float(np.min(stone_confidence))
            if board_quality < self.confidence_threshold.get():
                self._set_status(
                    "棋盘识别置信度不足",
                    f"棋盘定位/网格存活度最低 {board_quality:.0%}，低于门槛 {self.confidence_threshold.get():.0%}。请重新识别或手动框选。",
                )
                return False
            if stone_min < self._stone_threshold():
                if self._template_mode_active():
                    self._set_status(
                        "模板匹配相似度不足",
                        f"模板匹配最低交点相似度 {stone_min:.0%}，低于门槛 {self._stone_threshold():.0%}（滑块值的 60%）；"
                        "请核对参照图包是否覆盖该棋盘风格/分辨率，或切回颜色阈值对比。",
                    )
                else:
                    self._set_status(
                        "棋子识别置信度不足",
                        f"最低交点棋子置信度 {stone_min:.0%}，低于棋子门槛 {self._stone_threshold():.0%}（滑块值的 60%）。请重新识别或手动框选。",
                    )
                return False
            if not quiet:
                if self._template_mode_active():
                    self._set_status(
                        "棋盘已识别",
                        f"已锁定目标窗口网格（模板匹配）；定位 {geometry.confidence:.0%}，最低交点相似度 {stone_min:.0%}（门槛 {self._stone_threshold():.0%}）。",
                    )
                else:
                    self._set_status(
                        "棋盘已识别",
                        f"已锁定目标窗口网格（颜色阈值）；定位 {geometry.confidence:.0%}，最低交点 {stone_min:.0%}（棋子门槛 {self._stone_threshold():.0%}）。",
                    )
            return True
        except (DetectionError, OSError, ValueError) as exc:
            self._set_status("自动识别失败" if quiet else "识别失败", str(exc))
            return False

    def _template_mode_active(self) -> bool:
        """当前是否真的走模板匹配（识别方式为模板匹配且已导入图包）。

        未导入图包时 _read_board 会回退颜色阈值，此时提示必须按颜色阈值口径，
        否则会把颜色阈值的失败说成"模板匹配失败"，反而更难定位。
        """
        return self.recognition_mode.get() == "template" and self.template_lib is not None

    def _read_board(self, image_rgb: np.ndarray, geometry) -> tuple[np.ndarray, np.ndarray]:
        """按当前识别方式读取棋盘。

        默认“模板匹配”：已导入参照图包时走 read_board_template；
        未导入时自动回退颜色阈值 read_stones（保持原识别能力可用）。
        “颜色阈值”为保留路径：显式切换到该方式后始终走 read_stones。
        """
        if self.recognition_mode.get() == "template" and self.template_lib is not None:
            return read_board_template(image_rgb, geometry, self.template_lib)
        return read_stones(image_rgb, geometry)

    def _import_template_package(self) -> str:
        """导入参照图包。返回 "ok"；"cancelled"（未选择任何文件夹）；"invalid"（加载失败）。"""
        from tkinter import filedialog

        folder = filedialog.askdirectory(
            title="选择参照图包文件夹（含 meta.json 或 empty/black/white 子目录）"
        )
        if not folder:
            return "cancelled"
        try:
            library = load_template_package(folder)
        except ValueError as exc:
            self._set_status("导入模板失败", str(exc))
            return "invalid"
        self.template_lib = library
        self.recognition_mode.set("template")
        folder_name = os.path.basename(folder.rstrip("\\/"))
        self.template_folder_text.set(f"已导入：{folder_name}")
        self.template_status_text.set(f"模板参数：{library.summary().replace(' ', '')}")
        self._set_status(
            "参照图包已导入",
            f"{library.summary()}；识别方式已切换为模板匹配。",
        )
        return "ok"

    def _recognition_mode_changed(self) -> None:
        if self.recognition_mode.get() == "template":
            if self.template_lib is None:
                # 切换到模板匹配自动进入导入文件夹模式；导入失败/取消直接切回颜色阈值。
                result = self._import_template_package()
                if result != "ok":
                    self.recognition_mode.set("color")
                    self._recognition_mode_changed()
                    if result == "cancelled":
                        self.detail_text.set("导入失败")
                    else:
                        self.detail_text.set("导入文件夹内未找到 meta.json")
            else:
                self.template_status_text.set(f"模板参数：{self.template_lib.summary().replace(' ', '')}")
        else:
            # 切回颜色阈值：不保留已导入的文件夹；模板参数行冒号后空白。
            self.template_lib = None
            self.template_folder_text.set("未导入")
            self.template_status_text.set("模板参数：")
            self._set_status("识别方式：颜色阈值", "识别方式已切换为颜色阈值。")

    def calculate_move(self, quiet: bool = False) -> None:
        if not self.scan_screen(quiet=quiet):
            return
        self._start_engine_search("suggest")

    def _current_search_board(self) -> tuple[np.ndarray, int] | None:
        assert self.scan is not None
        if self._board_quality() < self.confidence_threshold.get():
            self._set_status("暂不计算", "棋盘定位置信度未达到门槛，请先校准。")
            return None
        if np.min(self.scan.stone_confidence) < self._stone_threshold():
            if self._template_mode_active():
                self._set_status(
                    "暂不计算",
                    f"模板匹配最低交点相似度 {float(np.min(self.scan.stone_confidence)):.0%} 未达门槛（滑块值的 60%），"
                    "请核对参照图包或切换识别方式。",
                )
            else:
                self._set_status("暂不计算", "棋子置信度未达到门槛（滑块值的 60%），请先校准。")
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
        target_conf = float(self.scan.stone_confidence[move.row, move.col])
        if target_conf < self._stone_threshold():
            if self._template_mode_active():
                self._set_status(
                    "目标格识别不可靠",
                    f"目标交点模板相似度 {target_conf:.0%} 不足（门槛为滑块值的 60%），取消本次点击。",
                )
            else:
                self._set_status(
                    "目标格识别不可靠",
                    f"目标交点棋子置信度 {target_conf:.0%} 不足（门槛为滑块值的 60%），取消本次点击。",
                )
            return False
        if time.monotonic() - self._last_click_at < 0.8:
            self._set_status("点击冷却中", "为防止重复落子，本次请求已拦截。")
            return False
        x, y = self.scan.geometry.screen_point(move.row, move.col)
        try:
            if self.click_mode.get() == "single_confirm":
                self._click_single_with_confirm(x, y)
            else:
                click_window(
                    self._target_hwnd,
                    x,
                    y,
                    clicks=2 if self.click_mode.get() == "double" else 1,
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

    def _click_single_with_confirm(self, x: int, y: int) -> None:
        """Single-click the intersection, then tap the yellow confirm button.

        五子棋达人 commits a move in two taps: the intersection, then a yellow
        confirm button under the board.  The button is located on the current
        frame by colour; a relative offset under the board centre is the
        fallback when the button is not (yet) visible.  The button position is
        resolved BEFORE clicking the intersection, so a short capture that
        cannot show the button fails the whole move instead of leaving a
        half-placed stone.
        """

        cancel = lambda: key_is_down(0x77)  # noqa: E731
        screen_btn: tuple[int, int] | None = None
        if self.scan is not None:
            geometry = self.scan.geometry
            # 手动定位：直接使用左侧视窗保存的确认按钮坐标；
            # 自动识别：黄色掩膜定位，定位失败时回退到棋盘下方固定偏移。
            if self.confirm_source.get() == "manual" and self.manual_confirm_xy is not None:
                img_x, img_y = self.manual_confirm_xy
                if 0 <= img_x < self.scan.image_rgb.shape[1] and 0 <= img_y < self.scan.image_rgb.shape[0]:
                    btn = (img_x, img_y)
                else:
                    raise OSError("手动确认按钮坐标已不在当前截图范围内；请重新点选。")
            else:
                btn = find_confirm_button(self.scan.image_rgb, geometry)
                if btn is None:
                    fallback_y = int(round(geometry.y_lines[-1] + geometry.spacing * 1.8))
                    if fallback_y < self.scan.image_rgb.shape[0] - 8:
                        btn = (
                            int(round((geometry.x_lines[0] + geometry.x_lines[-1]) / 2.0)),
                            fallback_y,
                        )
            if btn is not None:
                screen_btn = (
                    geometry.screen_left + btn[0],
                    geometry.screen_top + btn[1],
                )
        if screen_btn is None:
            raise OSError("确认按钮不在当前截图范围内，无法完成单击+确认落子；请拉高目标窗口后重试")
        click_window(self._target_hwnd, x, y, clicks=1, cancelled=cancel)
        time.sleep(0.15)
        click_window(self._target_hwnd, screen_btn[0], screen_btn[1], clicks=1, cancelled=cancel)

    def _confirm_source_changed(self) -> None:
        # 自动识别 <-> 手动定位 切换：切到手动定位后自动进入选择模式。
        if self.confirm_source.get() == "manual":
            self._enter_confirm_point_mode()
        else:
            self._exit_confirm_point_mode()
            self._set_status("确认按钮自动识别", "由黄色掩膜在棋盘下方自动定位确认按钮。")

    def _start_confirm_point_selection(self) -> None:
        # “选择”按钮：切到手动定位并进入选择模式。
        if self.confirm_source.get() != "manual":
            self.confirm_source.set("manual")
        self._enter_confirm_point_mode()

    def _side_auto_changed(self) -> None:
        # 勾选“识别执棋颜色”：进入执子图标定位模式，软件接管我执黑/我执白；
        # 取消勾选：退出定位模式，恢复手动选择与 F9。
        if self.side_auto.get():
            self._enter_stone_point_mode()
        else:
            self._exit_stone_point_mode()
            # 取消勾选：清除执子图标方框并重画画面（旧方框不残留）。
            # 用定位时保存的最新截图重画；scan 未生成时也必须重画，否则方框残留。
            self._stone_rect = None
            if self._pending_image is not None:
                self._display_image(self._pending_image, full=True)
            elif self.scan is not None:
                self._show_scan()
            self._set_status("执棋颜色手动选择", "已取消自动识别执棋；我执黑/我执白与 F9 恢复手动控制。")

    def _start_stone_point_selection(self) -> None:
        # “定位”按钮：未勾选时先勾选（自动进入定位模式），已勾选则重新选点。
        # 注意 side_auto.set() 不触发 command，必须显式进入定位模式以显示点选光标。
        if not self.side_auto.get():
            self.side_auto.set(True)
        self._enter_stone_point_mode()

    def _enter_stone_point_mode(self) -> None:
        # 进入执子图标定位模式：与确认按钮点选互斥；截最新帧显示，暂停自动模式，锁定后恢复。
        # 重新定位时先清除旧方框（上一轮识别结果作废，保存新坐标后重画）。
        self._exit_confirm_point_mode()
        self._stone_rect = None
        try:
            self._pending_image, self._pending_origin = self._capture_target()
        except OSError as exc:
            if self.side_auto.get():
                self.side_auto.set(False)
            self._set_status("无法定位执子图标", str(exc))
            return
        self._display_image(self._pending_image, full=True)
        if self.auto_enabled.get():
            self._stone_restore_auto = True
            self._stop_auto("正在点选执子图标；锁定后自动恢复。")
        self._stone_point_mode = True
        self.canvas.configure(cursor="crosshair")
        if self._stone_dot_id is None:
            r = self._confirm_dot_r
            self._stone_dot_id = self.canvas.create_oval(
                -r, -r, r, r, fill="#F0A020", outline="#8A5A00", width=2
            )
        self._set_status(
            "请点选执子图标",
            "已刷新窗口截图并暂停自动模式；把橙色圆点移到左上方执子图标中心左键保存，保存后自动恢复自动模式。",
        )

    def _exit_stone_point_mode(self) -> bool:
        """退出执子图标定位模式；若进入前自动模式开启则恢复开启。返回退出后自动模式是否开启。"""
        self._stone_point_mode = False
        if self._stone_dot_id is not None:
            try:
                self.canvas.delete(self._stone_dot_id)
            except tk.TclError:
                pass
            self._stone_dot_id = None
        self.canvas.configure(cursor="arrow")
        if self._stone_restore_auto:
            self._stone_restore_auto = False
            if not self.auto_enabled.get():
                self.auto_enabled.set(True)
                self._auto_changed()
        return self.auto_enabled.get()

    @staticmethod
    def _detect_icon_color(image_rgb: np.ndarray, cx: int, cy: int) -> str:
        """识别执子图标颜色：统计点击点周围中心核的深色像素占比。

        执子图标为圆形棋子标志：黑子中心深色占比显著（>8%），白子中心无深色。
        窗口半径按图宽缩放（约图标半径），中心核取 0.7×半径抗边缘干扰。
        """
        h, w = image_rgb.shape[:2]
        r = max(8, int(round(w * 0.028)))
        core = max(4, int(round(r * 0.7)))
        x0, x1 = max(0, cx - core), min(w - 1, cx + core)
        y0, y1 = max(0, cy - core), min(h - 1, cy + core)
        sub = image_rgb[y0:y1 + 1, x0:x1 + 1].astype(np.float64)
        gray_v = 0.299 * sub[..., 0] + 0.587 * sub[..., 1] + 0.114 * sub[..., 2]
        dark_ratio = float((gray_v < 90).mean())
        return "black" if dark_ratio >= 0.08 else "white"

    def _icon_radius_px(self, image_rgb: np.ndarray) -> int:
        return max(8, int(round(image_rgb.shape[1] * 0.028)))

    def _draw_stone_rect(self) -> None:
        # 在视窗上按当前视图变换重画执子图标方框（颜色按识别结果：黑/白）。
        if self._stone_rect is None or self._view_transform is None:
            return
        x0, y0, x1, y1, color = self._stone_rect
        scale, offset_x, offset_y, source_x, source_y = self._view_transform
        vx0 = (x0 - source_x) * scale + offset_x
        vy0 = (y0 - source_y) * scale + offset_y
        vx1 = (x1 - source_x) * scale + offset_x
        vy1 = (y1 - source_y) * scale + offset_y
        outline = "#FFFFFF" if color == "white" else "#000000"
        self.canvas.create_rectangle(vx0, vy0, vx1, vy1, outline=outline, width=3)

    def _refresh_stone_color(self) -> None:
        """持续识别执棋颜色：每次识别成功后检测执子图标，颜色变化则自动更新并重画方框。

        仅在“识别执棋颜色”开启且已定位时生效；检测点取方框中心，颜色与当前
        ai_color 不符时更新 _stone_rect / ai_color 并重置自动状态机（软件接管）。
        """
        if not self.side_auto.get() or self._stone_rect is None or self.scan is None:
            return
        cx = (self._stone_rect[0] + self._stone_rect[2]) // 2
        cy = (self._stone_rect[1] + self._stone_rect[3]) // 2
        color = self._detect_icon_color(self.scan.image_rgb, cx, cy)
        if color == self.ai_color.get():
            return
        x0, y0, x1, y1, _ = self._stone_rect
        self._stone_rect = (x0, y0, x1, y1, color)
        self.ai_color.set(color)
        self._player_changed()  # 重置自动状态机（side_auto 下与识别结果相符，走正常重置）
        self._show_scan()  # 重画方框（新颜色）
        label = "我执黑" if color == "black" else "我执白"
        self._set_status("执棋颜色已自动更新", f"执子图标检测为{label}，执棋颜色已由软件自动更新。")

    def _enter_confirm_point_mode(self) -> None:
        # 进入选择模式：先截取最新窗口帧显示到视窗（坐标基于最新截图），
        # 并暂停自动模式，坐标锁定后恢复。
        self._exit_stone_point_mode()  # 与执子图标点选互斥
        try:
            self._pending_image, self._pending_origin = self._capture_target()
        except OSError as exc:
            self._set_status("无法进入选择模式", str(exc))
            return
        self._display_image(self._pending_image, full=True)
        if self.auto_enabled.get():
            self._confirm_restore_auto = True
            self._stop_auto("正在点选确认按钮坐标；锁定后自动恢复。")
        self._confirm_point_mode = True
        self.canvas.configure(cursor="crosshair")
        if self._confirm_dot_id is None:
            r = self._confirm_dot_r
            self._confirm_dot_id = self.canvas.create_oval(
                -r, -r, r, r, fill="#F0A020", outline="#8A5A00", width=2
            )
        self._set_status(
            "请点选确认按钮",
            "已刷新窗口截图并暂停自动模式；把橙色圆点移到确认按钮中心左键保存，保存后自动恢复自动模式。",
        )

    def _exit_confirm_point_mode(self) -> bool:
        """退出选择模式；若进入前自动模式开启则恢复开启。返回退出后自动模式是否处于开启状态。"""
        self._confirm_point_mode = False
        if self._confirm_dot_id is not None:
            try:
                self.canvas.delete(self._confirm_dot_id)
            except tk.TclError:
                pass
            self._confirm_dot_id = None
        self.canvas.configure(cursor="arrow")
        if self._confirm_restore_auto:
            self._confirm_restore_auto = False
            if not self.auto_enabled.get():
                self.auto_enabled.set(True)
                self._auto_changed()
        return self.auto_enabled.get()

    def _confirm_motion(self, event: tk.Event) -> None:
        # 框选期间：未点击也显示绿色十字线并跟随鼠标；锚定后附加跟随正方形。
        if self._selecting and self._pending_image is not None:
            self._selection_motion(event)
            return
        # 确认按钮点选与执子图标点选共用橙色圆点跟随（画布被 _display_image 清空后重建）。
        dot_id = None
        if self._confirm_point_mode:
            dot_id = self._confirm_dot_id
        elif self._stone_point_mode:
            dot_id = self._stone_dot_id
        if dot_id is None:
            return
        r = self._confirm_dot_r
        try:
            self.canvas.coords(dot_id, event.x - r, event.y - r, event.x + r, event.y + r)
        except tk.TclError:
            new_id = self.canvas.create_oval(
                event.x - r, event.y - r, event.x + r, event.y + r,
                fill="#F0A020", outline="#8A5A00", width=2,
            )
            if self._confirm_point_mode:
                self._confirm_dot_id = new_id
            else:
                self._stone_dot_id = new_id

    def begin_manual_selection(self) -> None:
        # 手动框选与确认按钮/执子图标点选互斥：进入框选前退出点选模式。
        self._exit_confirm_point_mode()
        self._exit_stone_point_mode()
        # 手动框选棋盘不再停用自动模式：仅暂停采样点击，框选完成后自动恢复。
        if self.auto_enabled.get():
            self.auto_state.reset()
            self._engine_context = None
            self._pending_engine_result = None
            self._scan_failures = 0
            self._auto_suspended = True
        else:
            self.auto_state.reset()
        self._set_status("正在手动框选棋盘", "点击一个角，再点击对角；范围内应包含完整网格。")
        try:
            self._pending_image, self._pending_origin = self._capture_target()
        except OSError as exc:
            self._set_status("截图失败", str(exc))
            return
        self._reset_selection_state()
        self._selecting = True
        self.scan = None
        self.suggestion = None
        self.canvas.configure(cursor="crosshair")
        self._display_image(self._pending_image, full=True)
        # 未点击时即显示绿色十字线：逻辑光标先放在画面中心，随鼠标移动更新。
        height, width = self._pending_image.shape[:2]
        self._selection_cursor = (width / 2.0, height / 2.0)
        self._draw_selection_overlay()
        self.canvas.focus_set()  # 保证方向键/回车可用（防止焦点丢失）
        self._set_status("点击框选棋盘", "点击一个角，再点击对角；范围内应包含完整网格。")

    def _reset_selection_state(self) -> None:
        """重新框选/进入框选：清空上一轮的锚点、正方形与参考线（参考线不残留）。"""
        self._selection_anchor = None
        self._selection_square = None
        self._selection_cursor = None
        self._delete_selection_overlay()

    def _delete_selection_overlay(self) -> None:
        for item in (self._selection_id, self._xhair_h, self._xhair_v):
            if item is not None:
                try:
                    self.canvas.delete(item)
                except tk.TclError:
                    pass
        self._selection_id = None
        self._xhair_h = None
        self._xhair_v = None

    def _image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        if self._view_transform is None:
            return 0.0, 0.0
        scale, offset_x, offset_y, source_x, source_y = self._view_transform
        return (x - source_x) * scale + offset_x, (y - source_y) * scale + offset_y

    def _draw_selection_overlay(self) -> None:
        """按当前图像坐标状态重画框选 overlay：正方形框 + 贯穿整个画布的橙色十字线。"""
        if not self._selecting:
            return
        self._delete_selection_overlay()
        if self._selection_square is not None:
            x0, y0, x1, y1 = self._selection_square
            cx0, cy0 = self._image_to_canvas(x0, y0)
            cx1, cy1 = self._image_to_canvas(x1, y1)
            self._selection_id = self.canvas.create_rectangle(
                cx0, cy0, cx1, cy1, outline=self.COLORS["accent"], width=2
            )
        if self._selection_cursor is not None:
            cx, cy = self._image_to_canvas(*self._selection_cursor)
            width = max(1, self.canvas.winfo_width())
            height = max(1, self.canvas.winfo_height())
            self._xhair_h = self.canvas.create_line(
                0, cy, width, cy, fill="#4ADE80", dash=(6, 4), tags=("xhair",)
            )
            self._xhair_v = self.canvas.create_line(
                cx, 0, cx, height, fill="#4ADE80", dash=(6, 4), tags=("xhair",)
            )

    def _selection_motion(self, event: tk.Event) -> None:
        if not self._selecting or self._pending_image is None:
            return
        img_x, img_y = self._canvas_to_image(event.x, event.y)
        self._selection_cursor = (img_x, img_y)
        if self._selection_anchor is not None:
            box = make_square_box(self._selection_anchor, self._selection_cursor)
            if box is not None:
                self._selection_square = box
        self._draw_selection_overlay()

    def _selection_key(self, event: tk.Event) -> str:
        """方向键按图像坐标微调逻辑光标（Shift=10px）；回车前可反复微调。"""
        if not self._selecting or self._pending_image is None:
            return ""
        step = 10 if (event.state & 0x0001) else 1
        cx, cy = self._selection_cursor or self._selection_anchor or (0, 0)
        if event.keysym == "Left":
            cx -= step
        elif event.keysym == "Right":
            cx += step
        elif event.keysym == "Up":
            cy -= step
        elif event.keysym == "Down":
            cy += step
        height, width = self._pending_image.shape[:2]
        cx = max(0, min(width - 1, int(cx)))
        cy = max(0, min(height - 1, int(cy)))
        self._selection_cursor = (cx, cy)
        if self._selection_anchor is not None:
            box = make_square_box(self._selection_anchor, self._selection_cursor)
            if box is not None:
                self._selection_square = box
        self._draw_selection_overlay()
        return "break"

    def _selection_return(self, event: tk.Event) -> str:
        """回车 = 左键：第一击设角，第二击完成框选（防止人手按左键导致鼠标错位）。"""
        if not self._selecting or self._pending_image is None or self._selection_anchor is None:
            return ""
        self._finish_selection(make_square_box(self._selection_anchor, self._selection_cursor))
        return "break"

    def _selection_press(self, event: tk.Event) -> None:
        if self._stone_point_mode:
            # 执子图标定位模式：左键保存坐标并识别执棋颜色，软件接管我执黑/我执白。
            if self._view_transform is None or self._pending_image is None:
                return
            img_x, img_y = self._canvas_to_image(event.x, event.y)
            color = self._detect_icon_color(self._pending_image, img_x, img_y)
            r = self._icon_radius_px(self._pending_image)
            self._stone_rect = (img_x - r, img_y - r, img_x + r, img_y + r, color)
            self._exit_stone_point_mode()
            self.ai_color.set(color)  # 软件接管执棋颜色（触发 _player_changed 重置状态）
            self._draw_stone_rect()
            label = "我执黑" if color == "black" else "我执白"
            self._set_status(
                f"已识别执棋：{label}",
                f"执子图标已用{'黑色' if color == 'black' else '白色'}方框标注；执棋颜色由软件控制，取消勾选后可手动选择。",
            )
            return
        if self._confirm_point_mode:
            # 选择模式：左键即保存确认按钮坐标（视窗坐标 -> 图像坐标）。
            if self._view_transform is None:
                return
            img_x, img_y = self._canvas_to_image(event.x, event.y)
            self.manual_confirm_xy = (img_x, img_y)
            r = self._confirm_dot_r
            if self._confirm_dot_id is not None:
                try:
                    self.canvas.coords(
                        self._confirm_dot_id, event.x - r, event.y - r, event.x + r, event.y + r
                    )
                except tk.TclError:
                    self._confirm_dot_id = None
            auto_on = self._exit_confirm_point_mode()
            self._set_status(
                "确认按钮坐标已保存",
                f"手动坐标 ({img_x}, {img_y})；单击+确认将直接点击此处。"
                + ("自动模式已恢复运行。" if auto_on else "自动模式保持关闭。"),
            )
            return
        if not self._selecting or self._pending_image is None:
            return
        self.canvas.focus_set()  # 点击画布时重新抢焦点，保证方向键/回车可用
        img_x, img_y = self._canvas_to_image(event.x, event.y)
        if self._selection_anchor is None:
            # 第一击：清掉旧参考线，锚定该角；正方形与十字线随后续移动跟随。
            self._selection_anchor = (img_x, img_y)
            self._selection_square = None
            self._selection_cursor = (img_x, img_y)
            self._draw_selection_overlay()
            self._set_status("已选定第一个角", "移动鼠标（或方向键微调）后，点击/回车选定对角。")
        else:
            # 第二击：完成框选并识别。
            self._finish_selection(make_square_box(self._selection_anchor, (img_x, img_y)))

    def _selection_drag(self, event: tk.Event) -> None:
        # 点击式拉框不使用拖动；保留空实现以兼容旧绑定。
        return

    def _canvas_to_image(self, x: float, y: float) -> tuple[int, int]:
        if self._view_transform is None:
            return 0, 0
        scale, offset_x, offset_y, source_x, source_y = self._view_transform
        img_x = int(round((x - offset_x) / scale + source_x))
        img_y = int(round((y - offset_y) / scale + source_y))
        if self._pending_image is not None:
            height, width = self._pending_image.shape[:2]
            img_x = max(0, min(width - 1, img_x))
            img_y = max(0, min(height - 1, img_y))
        return img_x, img_y

    def _finish_selection(self, box: tuple[int, int, int, int] | None) -> None:
        if box is None or self._pending_image is None:
            return
        self._selection_square = box
        self._draw_selection_overlay()  # 先显示最终正方形供核对
        try:
            geometry = geometry_from_rect(self._pending_image, box, self.board_size.get())
            self._locked_geometry = with_screen_origin(geometry, (0, 0))
            self._locked_capture_shape = self._pending_image.shape[:2]
            screen_geometry = with_screen_origin(self._locked_geometry, self._pending_origin)
            liveness = grid_liveness(self._pending_image, screen_geometry)
            board, stone_confidence = self._read_board(self._pending_image, screen_geometry)
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
            self._delete_selection_overlay()
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
            self._selecting = True
            self.canvas.configure(cursor="crosshair")
            self._reset_selection_state()
            return
        finally:
            self._selection_anchor = None
            self._selection_square = None
            self._selection_cursor = None
            # 框选结束（无论成功与否）恢复自动采样；自动模式保持开启。
            if self._auto_suspended:
                self._auto_suspended = False
                if self.auto_enabled.get() and self._auto_job is None:
                    self._schedule_auto(80)

    def _selection_release(self, event: tk.Event) -> None:
        # 点击式拉框在第二击/回车时即完成；release 无需处理（保留空实现兼容旧绑定）。
        return

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
            # 完整显示窗口截图（含棋盘上下与确认按钮），不裁剪为棋盘特写。
            self._display_image(self._annotated_image(), full=True)

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
        self._draw_stone_rect()  # 画面刷新后持续标注执子图标方框（黑/白）
        if self._selecting:
            self._draw_selection_overlay()  # 画面重建后恢复框选 overlay（正方形 + 贯穿十字线）

    def _click_permission_changed(self) -> None:
        # 关闭点击权限不再停用自动模式：自动保持开启，仅不执行点击，重新勾选后自动恢复。
        state = "已开启" if self.click_allowed.get() else "已关闭"
        if not self.click_allowed.get() and self.auto_enabled.get():
            self._set_status(f"鼠标点击{state}", "自动模式保持运行但不会执行点击；重新勾选“允许鼠标点击”后自动恢复。")
        else:
            self._set_status(f"鼠标点击{state}")

    def _update_auto_button(self) -> None:
        # 自动识别并代下开关按钮颜色：关闭=灰，开启=亮灰（接近白）。
        if not hasattr(self, "auto_toggle_btn"):
            return
        self.auto_toggle_btn.configure(
            style="AutoOn.TButton" if self.auto_enabled.get() else "AutoOff.TButton"
        )

    def _auto_changed(self) -> None:
        if self.auto_enabled.get() and not self.click_allowed.get():
            self.auto_enabled.set(False)
            self.root.bell()
            self._update_auto_button()
            self._set_status("无法开启自动模式", "必须先开启“允许鼠标点击”。")
            return
        if self.auto_enabled.get():
            if self._target_hwnd is None:
                self.auto_enabled.set(False)
                self._update_auto_button()
                self._set_status("无法开启自动模式", "请先选择棋局窗口。")
                return
            self.auto_state.reset()
            self._scan_failures = 0
            self._update_auto_button()
            self._set_status("自动模式已启动", "仅在识别可靠且轮到你方时点击；按 F8 可立即停止。")
            self._schedule_auto(80)
        else:
            self.auto_state.reset()
            self._update_auto_button()
            self._set_status("自动模式已停止")

    def _toggle_auto_mode(self) -> None:
        # F10：开启/关闭自动模式。复用 _auto_changed 的完整校验与启动/停止逻辑，
        # 开启失败（未开点击权限 / 未选窗口）时由 _auto_changed 回滚勾选并提示。
        if not self.auto_enabled.get() and not self.click_allowed.get():
            self.root.bell()
            self._set_status("无法开启自动模式", "必须先开启“允许鼠标点击”。")
            return
        self.auto_enabled.set(not self.auto_enabled.get())
        self._auto_changed()

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
        self._pending_engine_result = None
        if reset_state:
            self.auto_state.reset()
        self._scan_failures = 0
        self._update_auto_button()
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
                # 引擎计算失败不再停用自动模式：丢弃本次结果，继续采样识别，
                # 盘面稳定且轮到我方时下一轮会自动重试计算。
                self._pending_engine_result = None
                self._set_status("强引擎计算失败", f"{exc}。将持续识别，盘面稳定后自动重试计算。")
                self._resume_auto_after_engine(80)
            elif action is not None:
                self._set_status("强引擎计算失败", f"{exc}。为避免弱棋或误点，本次不会使用旧引擎代替。")
                self._resume_auto_after_engine()
            return
        except Exception as exc:
            action = context[3] if context is not None else None
            if action == "auto" and self.auto_enabled.get():
                # 引擎线程异常同样不再停用自动模式，继续采样识别等待恢复。
                self._pending_engine_result = None
                self._set_status("强引擎线程异常", f"{exc}。将持续识别，盘面稳定后自动重试计算。")
                self._resume_auto_after_engine(80)
            elif action is not None:
                self._set_status("强引擎线程异常", str(exc))
                self._resume_auto_after_engine()
            return

        if context is None:
            self._resume_auto_after_engine()
            return
        self._deliver_engine_result(move, context)

    def _deliver_engine_result(
        self,
        move: MoveAnalysis,
        context: tuple[bytes, object | None, int, str],
    ) -> None:
        """Turn a finished engine result into a confirmed board check and a click.

        Re-confirming the board after Rapfi finishes may fail for a moment
        (capture glitches / black blocks).  In auto mode the result is parked
        and retried by the next 0.5 s sampling tick, up to 3 consecutive
        failures, instead of aborting the auto mode on the first miss.
        """

        signature, geometry_key, player, action = context
        if action == "auto" and (not self.auto_enabled.get() or not self.click_allowed.get()):
            return
        if action == "execute" and not self.click_allowed.get():
            self._set_status("点击权限已关闭", "Rapfi 思考完成，但点击授权已撤销，未执行落子。")
            self._resume_auto_after_engine()
            return
        if player != self._player():
            if action == "auto":
                # 思考期间执棋颜色变化不再停用自动模式：丢弃旧落点，重置状态后继续识别。
                self.auto_state.reset()
                self._pending_engine_result = None
                self._set_status("执棋颜色已变化", "Rapfi 思考期间执棋颜色被更改；旧落点已丢弃，将持续识别。")
                self._resume_auto_after_engine(80)
            else:
                self._set_status("已丢弃旧落点", "思考期间执棋颜色发生变化。")
                self._resume_auto_after_engine()
            return
        if not self.scan_screen(quiet=True) or self.scan is None:
            if action == "auto":
                # 确认棋盘失败不再停用自动模式：暂存落点结果，由后续采样循环无限重试，
                # 直到能重新确认棋盘为止（持续重新识别窗口）。
                self._scan_failures += 1
                self._pending_engine_result = (move, context)
                self._set_status(
                    "等待重新识别",
                    f"Rapfi 已算好落点，棋盘暂时无法确认（已重试 {self._scan_failures} 次），将持续重试。",
                )
                self._resume_auto_after_engine(80)
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
                # 落点被拦截（权限/盘面变化/冷却等）不再停用自动模式：继续采样识别，等待下一机会。
                self._set_status("落点未能安全点击", "本次落点被安全拦截，将持续识别等待下一机会。")
                self._resume_auto_after_engine(80)
            else:
                self._resume_auto_after_engine()
            return
        if action == "auto":
            self._resume_auto_after_engine(int(self.interval_seconds.get() * 1000))
        else:
            self._resume_auto_after_engine()

    def _auto_tick(self) -> None:
        self._auto_job = None
        try:
            if not self.auto_enabled.get():
                return  # 仅手动关闭勾选 / F8 会走到这里，采样循环随之停止
            if self._auto_suspended:
                return  # 手动框选棋盘期间暂停采样点击，循环保持运行，框选完成后恢复
            if not self.click_allowed.get():
                return  # 点击权限关闭时保持采样但不点击，也不停用自动模式，恢复后自动续点
            valid = self.scan_screen(quiet=True)
            if not valid or self.scan is None:
                # 识别失败不再停用自动模式：持续重新识别窗口，直到恢复。
                self._scan_failures += 1
                self._set_status("等待重新识别", f"识别暂时失败（已重试 {self._scan_failures} 次），将持续重试。")
                return
            self._scan_failures = 0
            if self._pending_engine_result is not None:
                move, context = self._pending_engine_result
                self._pending_engine_result = None
                self._deliver_engine_result(move, context)
                return
            observation = self.auto_state.observe(
                self.scan.board,
                self._player(),
                time.monotonic(),
                self._geometry_key(),
            )
            if observation.decision == AutoDecision.ERROR:
                # 盘面校验异常（棋子计数/落子确认等）不再停用自动模式：
                # 重置状态后继续采样，等待盘面恢复正常。
                self.auto_state.reset()
                self._pending_engine_result = None
                self._set_status("盘面校验异常", f"{observation.message}；将持续识别等待盘面恢复。")
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
            # 自动循环异常不再停用自动模式：丢弃暂存结果，继续采样识别。
            self._pending_engine_result = None
            self._set_status("自动循环异常", f"{exc}；将持续尝试重新识别。")
        finally:
            # Keep sampling at the configured rate (0.5 s) even while Rapfi is
            # thinking, so a capture glitch right after the search finishes can
            # be recovered by the next tick instead of stalling the loop.
            if self.auto_enabled.get() and self._auto_job is None:
                self._schedule_auto()

    def _poll_emergency_key(self) -> None:
        down = key_is_down(0x77)  # F8
        if down and not self._f8_was_down:
            self._stop_auto("F8 急停已触发；自动模式和鼠标点击权限均已关闭。", revoke_click=True)
        self._f8_was_down = down
        toggle = key_is_down(0x78)  # F9：切换我执黑/白
        if toggle and not self._f9_was_down:
            self._toggle_player()
        self._f9_was_down = toggle
        toggle10 = key_is_down(0x79)  # F10：开启/关闭自动模式
        if toggle10 and not self._f10_was_down:
            self._toggle_auto_mode()
        self._f10_was_down = toggle10
        self.root.after(80, self._poll_emergency_key)

    def _toggle_player(self) -> None:
        if self.side_auto.get():
            # F9 在“识别执棋颜色”开启时停用：执棋颜色完全由软件控制。
            self._set_status("F9 已停用", "执棋颜色由软件自动识别控制；请先取消勾选“识别执棋颜色”。")
            return
        new_color = "white" if self.ai_color.get() == "black" else "black"
        self.ai_color.set(new_color)
        self._player_changed()
        label = "我执黑" if new_color == "black" else "我执白"
        self._set_status("执棋已切换", f"F9：已切换为{label}；自动模式保持运行。")

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
