from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from .engine import BLACK, EMPTY, WHITE, GomokuEngine, MoveAnalysis


_MOVE_RE = re.compile(r"^(\d+)\s*,\s*(\d+)$")
_IGNORED_PREFIXES = ("MESSAGE", "DEBUG", "INFO", "SUGGEST")


class RapfiError(RuntimeError):
    """Raised when the external strong engine cannot safely return a move."""


class RapfiEngine:
    """Persistent Gomocup-protocol adapter for the bundled Rapfi engine."""

    RELEASE = "250615"
    DISPLAY_NAME = "Rapfi 2025-06-15"

    def __init__(
        self,
        engine_dir: str | os.PathLike[str] | None = None,
        *,
        think_time_ms: int = 5_000,
        threads: int = 2,
        memory_mb: int = 256,
    ) -> None:
        default_dir = Path(__file__).resolve().parents[1] / "vendor" / "rapfi"
        self.engine_dir = Path(engine_dir).resolve() if engine_dir is not None else default_dir
        self.think_time_ms = self._clamp_think_time(think_time_ms)
        self.threads = max(1, min(int(threads), 16))
        self.memory_mb = max(64, min(int(memory_mb), 2_048))
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._board_size: int | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _clamp_think_time(value: int) -> int:
        return max(100, min(int(value), 30_000))

    def set_think_time_ms(self, value: int) -> None:
        self.think_time_ms = self._clamp_think_time(value)

    @staticmethod
    def _has_avx2() -> bool:
        if os.name != "nt":
            return False
        try:
            # PF_AVX2_INSTRUCTIONS_AVAILABLE. Windows also verifies OS save-state support.
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _has_sse41() -> bool:
        if os.name != "nt":
            return False
        try:
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(37))
        except (AttributeError, OSError):
            return False

    def _resolve_executable(self) -> Path:
        override = os.environ.get("GOMOKU_RAPFI_EXE")
        if override:
            executable = Path(override).expanduser().resolve()
            if not executable.is_file():
                raise RapfiError(f"GOMOKU_RAPFI_EXE 指向的文件不存在：{executable}")
            return executable

        if self._has_avx2():
            names = ("pbrain-rapfi-windows-avx2.exe", "pbrain-rapfi-windows-sse.exe")
        elif self._has_sse41():
            names = ("pbrain-rapfi-windows-sse.exe",)
        else:
            raise RapfiError("当前 CPU 不支持 Rapfi 所需的 AVX2 或 SSE4.1 指令集")
        for name in names:
            executable = self.engine_dir / name
            if executable.is_file():
                return executable
        raise RapfiError("Rapfi 强引擎文件缺失，请重新解压完整项目")

    def _check_runtime_files(self) -> None:
        required = (
            "config.toml",
            "model210901.bin",
            "mix9svqfreestyle_bsmix.bin.lz4",
        )
        missing = [name for name in required if not (self.engine_dir / name).is_file()]
        if missing:
            raise RapfiError(f"Rapfi 运行文件缺失：{', '.join(missing)}")

    @staticmethod
    def _reader_loop(process: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
        assert process.stdout is not None
        try:
            for line in iter(process.stdout.readline, ""):
                lines.put(line.strip())
        finally:
            lines.put(None)

    def _send_lines(self, lines: list[str] | tuple[str, ...]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RapfiError("Rapfi 进程未运行")
        try:
            process.stdin.write("\n".join(lines) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RapfiError("无法向 Rapfi 发送棋盘") from exc

    def _read_line(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty as exc:
            raise RapfiError("等待 Rapfi 响应超时") from exc
        if line is None:
            code = self._process.poll() if self._process is not None else None
            raise RapfiError(f"Rapfi 进程意外退出（代码 {code}）")
        return line

    def _wait_for_ok(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RapfiError("等待 Rapfi 初始化超时")
            line = self._read_line(remaining)
            if line == "OK":
                return
            upper = line.upper()
            if upper.startswith(("ERROR", "UNKNOWN")):
                raise RapfiError(f"Rapfi 初始化失败：{line}")

    def _wait_for_move(self, timeout: float) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RapfiError("等待 Rapfi 落点超时")
            line = self._read_line(remaining)
            match = _MOVE_RE.fullmatch(line)
            if match:
                return int(match.group(1)), int(match.group(2))
            upper = line.upper()
            if upper.startswith(("ERROR", "UNKNOWN")):
                raise RapfiError(f"Rapfi 拒绝当前棋盘：{line}")
            if line and not upper.startswith(_IGNORED_PREFIXES):
                raise RapfiError(f"Rapfi 返回了无法识别的响应：{line}")

    def _start(self, board_size: int) -> None:
        self._check_runtime_files()
        executable = self._resolve_executable()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(self.engine_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RapfiError(f"无法启动 Rapfi：{exc}") from exc

        self._process = process
        self._lines = queue.Queue()
        self._reader = threading.Thread(
            target=self._reader_loop,
            args=(process, self._lines),
            name="rapfi-output",
            daemon=True,
        )
        self._reader.start()
        try:
            self._send_lines([f"START {board_size}"])
            self._wait_for_ok(8.0)
        except RapfiError:
            self._shutdown(force=True)
            raise
        self._board_size = board_size

    def _ensure_started(self, board_size: int) -> None:
        process = self._process
        if process is not None and process.poll() is None and self._board_size == board_size:
            return
        self._shutdown(force=True)
        self._start(board_size)

    def _shutdown(self, *, force: bool = False) -> None:
        process = self._process
        reader = self._reader
        self._process = None
        self._reader = None
        self._board_size = None
        if process is None:
            return
        if process.poll() is None and not force and process.stdin is not None:
            try:
                process.stdin.write("END\n")
                process.stdin.flush()
                process.wait(timeout=0.8)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if reader is not None and reader.is_alive():
            reader.join(timeout=0.2)

    @staticmethod
    def _board_lines(board: np.ndarray, player: int) -> list[str]:
        lines = ["BOARD"]
        own = [(int(row), int(col)) for row, col in np.argwhere(board == player)]
        opponent = [(int(row), int(col)) for row, col in np.argwhere(board == -player)]

        # Rapfi uses BOARD input order to derive selfColor/sideToMove. The exact
        # history is unavailable from a screenshot, but freestyle positions are
        # history-independent, so any legal alternation ending with the opponent
        # preserves the position and makes Rapfi search for the requested player.
        if player == BLACK and len(own) == len(opponent):
            ordered = [item for pair in zip(own, opponent) for item in ((pair[0], 1), (pair[1], 2))]
        elif player == WHITE and len(opponent) == len(own) + 1:
            ordered = []
            for index, own_stone in enumerate(own):
                ordered.extend(((opponent[index], 2), (own_stone, 1)))
            ordered.append((opponent[-1], 2))
        else:
            raise ValueError("棋子数量与当前引擎执棋方不一致")

        for (row, col), marker in ordered:
            lines.append(f"{col},{row},{marker}")
        lines.append("DONE")
        return lines

    @staticmethod
    def winner(board: np.ndarray) -> int:
        return GomokuEngine.winner(board)

    @staticmethod
    def _validate_move(board: np.ndarray, col: int, row: int) -> tuple[int, int]:
        size = board.shape[0]
        if not (0 <= row < size and 0 <= col < size):
            raise RapfiError(f"Rapfi 返回越界坐标：{col},{row}")
        if board[row, col] != EMPTY:
            raise RapfiError(f"Rapfi 返回了非空交点：{col},{row}")
        return row, col

    def analyze(self, board: np.ndarray, player: int) -> MoveAnalysis:
        board = GomokuEngine._validated_board(board)
        if board.shape[0] not in (15, 19):
            raise ValueError("Rapfi 仅用于当前助手支持的 15 路或 19 路棋盘")
        if player not in (BLACK, WHITE):
            raise ValueError("player 必须是 BLACK 或 WHITE")
        if GomokuEngine._winner_unchecked(board) != EMPTY:
            raise ValueError("棋局已经结束")
        if not np.any(board == EMPTY):
            raise ValueError("棋盘已满")

        with self._lock:
            started = time.perf_counter()
            think_time_ms = self.think_time_ms
            try:
                self._ensure_started(board.shape[0])
                time_left_ms = max(30_000, think_time_ms * 60)
                self._send_lines(
                    [
                        "INFO rule 0",
                        f"INFO timeout_turn {think_time_ms}",
                        f"INFO time_left {time_left_ms}",
                        f"INFO max_memory {self.memory_mb * 1024 * 1024}",
                        f"INFO thread_num {self.threads}",
                    ]
                )
                self._send_lines(self._board_lines(board, player))
                col, row = self._wait_for_move(max(8.0, think_time_ms / 1000 + 5.0))
                row, col = self._validate_move(board, col, row)
            except RapfiError:
                self._shutdown(force=True)
                raise

        elapsed = time.perf_counter() - started
        return MoveAnalysis(row, col, 0.0, f"{self.DISPLAY_NAME} 强引擎，搜索 {elapsed:.2f} 秒")

    def best_move(self, board: np.ndarray, player: int) -> tuple[int, int]:
        analysis = self.analyze(board, player)
        return analysis.row, analysis.col

    def close(self) -> None:
        with self._lock:
            self._shutdown()

    def abort(self) -> None:
        """Interrupt a search without waiting for the protocol lock.

        This is only used while the UI is closing. Terminating the process makes
        the reader wake immediately; the search thread then performs normal
        locked cleanup.
        """
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def __enter__(self) -> RapfiEngine:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
