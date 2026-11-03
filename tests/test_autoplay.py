import unittest

import numpy as np

from gomoku_assistant.autoplay import AutoDecision, AutoPlayState
from gomoku_assistant.engine import BLACK, WHITE


class AutoPlayTests(unittest.TestCase):
    def test_requires_two_stable_reads_before_click(self) -> None:
        state = AutoPlayState(stable_reads=2)
        board = np.zeros((15, 15), dtype=np.int8)
        self.assertEqual(state.observe(board, BLACK, 0).decision, AutoDecision.WAIT_STABLE)
        self.assertEqual(state.observe(board, BLACK, 0.1).decision, AutoDecision.READY)

    def test_waits_for_opponent_turn(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 7] = BLACK
        self.assertEqual(state.observe(board, BLACK, 0).decision, AutoDecision.WAIT_TURN)
        self.assertEqual(state.observe(board, WHITE, 0.1).decision, AutoDecision.READY)

    def test_click_must_be_confirmed_at_exact_target(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        self.assertEqual(state.observe(board, BLACK, 0.2).decision, AutoDecision.CLICK_PENDING)
        board[7, 7] = BLACK
        self.assertEqual(state.observe(board, BLACK, 0.3).decision, AutoDecision.CLICK_CONFIRMED)

    def test_unexpected_change_fails_closed_after_three_reads(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        changed = board.copy()
        changed[5, 5] = BLACK
        self.assertEqual(state.observe(changed, BLACK, 0.2).decision, AutoDecision.CLICK_PENDING)
        self.assertEqual(state.observe(changed, BLACK, 0.3).decision, AutoDecision.CLICK_PENDING)
        result = state.observe(changed, BLACK, 0.4)
        self.assertEqual(result.decision, AutoDecision.ERROR)

    def test_target_plus_extra_own_stone_fails_closed_after_three_reads(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        changed = board.copy()
        changed[7, 7] = BLACK
        changed[6, 6] = BLACK
        self.assertEqual(state.observe(changed, BLACK, 0.2).decision, AutoDecision.CLICK_PENDING)
        self.assertEqual(state.observe(changed, BLACK, 0.3).decision, AutoDecision.CLICK_PENDING)
        self.assertEqual(state.observe(changed, BLACK, 0.4).decision, AutoDecision.ERROR)

    def test_target_plus_one_fast_opponent_reply_is_confirmed(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        changed = board.copy()
        changed[7, 7] = BLACK
        changed[6, 7] = WHITE
        self.assertEqual(state.observe(changed, BLACK, 0.2).decision, AutoDecision.CLICK_CONFIRMED)

    def test_single_illegal_frame_waits_for_stability(self) -> None:
        state = AutoPlayState(stable_reads=2)
        noisy = np.zeros((15, 15), dtype=np.int8)
        noisy[2, 2] = WHITE
        self.assertEqual(state.observe(noisy, BLACK, 0).decision, AutoDecision.WAIT_STABLE)
        clean = np.zeros((15, 15), dtype=np.int8)
        self.assertEqual(state.observe(clean, BLACK, 0.1).decision, AutoDecision.WAIT_STABLE)
        self.assertEqual(state.observe(clean, BLACK, 0.2).decision, AutoDecision.READY)

    def test_geometry_change_while_pending_fails_closed(self) -> None:
        state = AutoPlayState(stable_reads=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0, geometry_key="board-a")
        state.mark_clicked(board, (7, 7), BLACK, 0.1, geometry_key="board-a")
        result = state.observe(board, BLACK, 0.2, geometry_key="board-b")
        self.assertEqual(result.decision, AutoDecision.ERROR)

    def test_click_timeout_fails_closed(self) -> None:
        state = AutoPlayState(stable_reads=1, click_timeout=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        result = state.observe(board, BLACK, 1.2)
        self.assertEqual(result.decision, AutoDecision.ERROR)

    def test_click_timeout_cannot_be_postponed_by_alternating_noise(self) -> None:
        state = AutoPlayState(stable_reads=2, click_timeout=1)
        board = np.zeros((15, 15), dtype=np.int8)
        state.observe(board, BLACK, 0)
        state.mark_clicked(board, (7, 7), BLACK, 0.1)
        noise_a = board.copy()
        noise_a[2, 2] = WHITE
        noise_b = board.copy()
        noise_b[3, 3] = WHITE

        self.assertEqual(state.observe(noise_a, BLACK, 0.2).decision, AutoDecision.CLICK_PENDING)
        self.assertEqual(state.observe(noise_b, BLACK, 0.7).decision, AutoDecision.CLICK_PENDING)
        self.assertEqual(state.observe(noise_a, BLACK, 1.2).decision, AutoDecision.ERROR)


if __name__ == "__main__":
    unittest.main()
