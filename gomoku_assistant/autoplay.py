from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from .engine import BLACK, EMPTY, WHITE


class AutoDecision(str, Enum):
    WAIT_STABLE = "wait_stable"
    WAIT_TURN = "wait_turn"
    READY = "ready"
    CLICK_PENDING = "click_pending"
    CLICK_CONFIRMED = "click_confirmed"
    ERROR = "error"


@dataclass(frozen=True)
class AutoObservation:
    decision: AutoDecision
    message: str


class AutoPlayState:
    """Fail-closed state for turning repeated board scans into one click."""

    def __init__(self, stable_reads: int = 2, click_timeout: float = 3.5) -> None:
        self.required_stable_reads = max(1, stable_reads)
        self.click_timeout = max(1.0, click_timeout)
        self._last_signature: object | None = None
        self._stable_count = 0
        self._pending_move: tuple[int, int] | None = None
        self._pending_player: int | None = None
        self._pending_signature: bytes | None = None
        self._pending_board: np.ndarray | None = None
        self._pending_geometry: object | None = None
        self._pending_since = 0.0
        self._pending_observation_key: object | None = None
        self._pending_observation_count = 0
        self._blocked_reason: str | None = None

    @staticmethod
    def signature(board: np.ndarray) -> bytes:
        return np.asarray(board, dtype=np.int8).tobytes()

    @staticmethod
    def turn(board: np.ndarray) -> int | None:
        black = int(np.count_nonzero(board == BLACK))
        white = int(np.count_nonzero(board == WHITE))
        if black == white:
            return BLACK
        if black == white + 1:
            return WHITE
        return None

    @property
    def pending_move(self) -> tuple[int, int] | None:
        return self._pending_move

    @property
    def blocked_reason(self) -> str | None:
        return self._blocked_reason

    def reset(self) -> None:
        self._last_signature = None
        self._stable_count = 0
        self._pending_move = None
        self._pending_player = None
        self._pending_signature = None
        self._pending_board = None
        self._pending_geometry = None
        self._pending_since = 0.0
        self._pending_observation_key = None
        self._pending_observation_count = 0
        self._blocked_reason = None

    def mark_clicked(
        self,
        board: np.ndarray,
        move: tuple[int, int],
        player: int,
        now: float,
        geometry_key: object | None = None,
    ) -> None:
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        row, col = move
        if board[row, col] != EMPTY:
            raise ValueError("目标交点已经有棋子")
        self._pending_move = move
        self._pending_player = player
        self._pending_signature = self.signature(board)
        self._pending_board = np.asarray(board, dtype=np.int8).copy()
        self._pending_geometry = geometry_key
        self._pending_since = now
        self._pending_observation_key = None
        self._pending_observation_count = 0
        self._stable_count = 0

    def observe(
        self,
        board: np.ndarray,
        player: int,
        now: float,
        geometry_key: object | None = None,
    ) -> AutoObservation:
        board = np.asarray(board, dtype=np.int8)
        signature = self.signature(board)
        if self._blocked_reason:
            return AutoObservation(AutoDecision.ERROR, self._blocked_reason)

        if self._pending_move is not None:
            if geometry_key != self._pending_geometry:
                self._blocked_reason = "等待落子确认时棋盘位置发生变化，自动模式持续识别中（不会自动退出）"
                return AutoObservation(AutoDecision.ERROR, self._blocked_reason)
            row, col = self._pending_move
            current_key = (signature, geometry_key)
            if current_key == self._pending_observation_key:
                self._pending_observation_count += 1
            else:
                self._pending_observation_key = current_key
                self._pending_observation_count = 1
            if int(board[row, col]) == self._pending_player:
                assert self._pending_board is not None
                changed = np.argwhere(board != self._pending_board)
                allowed = True
                opponent_additions = 0
                for changed_row, changed_col in changed:
                    old = int(self._pending_board[changed_row, changed_col])
                    new = int(board[changed_row, changed_col])
                    if (int(changed_row), int(changed_col)) == (row, col):
                        allowed &= old == EMPTY and new == self._pending_player
                    elif old == EMPTY and new == -self._pending_player:
                        opponent_additions += 1
                    else:
                        allowed = False
                allowed &= opponent_additions <= 1
                if not allowed:
                    if self._pending_observation_count >= max(3, self.required_stable_reads):
                        self._blocked_reason = "落子确认期间出现删除、翻色或多枚额外棋子，自动模式持续识别中（不会自动退出）"
                        return AutoObservation(AutoDecision.ERROR, self._blocked_reason)
                    return AutoObservation(
                        AutoDecision.CLICK_PENDING,
                        f"等待落子动画稳定（{self._pending_observation_count}/3）",
                    )
                self._pending_move = None
                self._pending_player = None
                self._pending_signature = None
                self._pending_board = None
                self._pending_geometry = None
                self._pending_observation_key = None
                self._pending_observation_count = 0
                self._last_signature = (signature, geometry_key)
                self._stable_count = 1
                return AutoObservation(AutoDecision.CLICK_CONFIRMED, "落子已在棋盘上确认")
            if now - self._pending_since > self.click_timeout:
                self._blocked_reason = "点击后目标格长时间没有出现棋子，自动模式持续识别中（不会自动退出）"
                return AutoObservation(AutoDecision.ERROR, self._blocked_reason)
            if signature != self._pending_signature:
                if self._pending_observation_count >= max(3, self.required_stable_reads):
                    self._blocked_reason = "点击后棋盘发生了意外变化，目标格未出现我方棋子，自动模式持续识别中（不会自动退出）"
                    return AutoObservation(AutoDecision.ERROR, self._blocked_reason)
                return AutoObservation(
                    AutoDecision.CLICK_PENDING,
                    f"等待落子动画稳定（{self._pending_observation_count}/3）",
                )
            return AutoObservation(
                AutoDecision.CLICK_PENDING,
                f"等待游戏确认刚才的落子（{self._pending_observation_count}/3）",
            )

        observation_key = (signature, geometry_key)
        if observation_key == self._last_signature:
            self._stable_count += 1
        else:
            self._last_signature = observation_key
            self._stable_count = 1

        if self._stable_count < self.required_stable_reads:
            return AutoObservation(AutoDecision.WAIT_STABLE, "等待棋盘画面稳定")
        turn = self.turn(board)
        if turn is None:
            self._blocked_reason = "黑白棋子数量不符合合法回合，自动模式持续识别中（不会自动退出）"
            return AutoObservation(AutoDecision.ERROR, self._blocked_reason)
        if turn != player:
            return AutoObservation(AutoDecision.WAIT_TURN, "等待对方落子")
        return AutoObservation(AutoDecision.READY, "棋盘稳定且轮到我方")
