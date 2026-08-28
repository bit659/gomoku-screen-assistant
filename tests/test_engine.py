import unittest
from time import perf_counter

import numpy as np

from gomoku_assistant.engine import BLACK, WHITE, GomokuEngine


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = GomokuEngine()

    def test_empty_board_uses_center(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        self.assertEqual(self.engine.best_move(board, BLACK), (7, 7))

    def test_finishes_open_four(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 4:8] = BLACK
        move = self.engine.best_move(board, BLACK)
        self.assertIn(move, ((7, 3), (7, 8)))

    def test_own_win_takes_priority_over_blocking(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[4, 3:7] = BLACK
        board[9, 3:7] = WHITE

        self.assertIn(self.engine.best_move(board, BLACK), ((4, 2), (4, 7)))

    def test_blocks_opponent_win(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[8, 5:9] = WHITE
        move = self.engine.best_move(board, BLACK)
        self.assertIn(move, ((8, 4), (8, 9)))

    def test_blocks_broken_four_and_boundary_four(self) -> None:
        broken = np.zeros((15, 15), dtype=np.int8)
        broken[7, (3, 4, 6, 7)] = WHITE
        self.assertEqual(self.engine.best_move(broken, BLACK), (7, 5))

        boundary = np.zeros((15, 15), dtype=np.int8)
        boundary[0, 0:4] = WHITE
        self.assertEqual(self.engine.best_move(boundary, BLACK), (0, 4))

    def test_creates_open_four_for_forced_win(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, (4, 5, 7)] = BLACK
        board[3, 3] = WHITE

        analysis = self.engine.analyze(board, BLACK)

        self.assertEqual((analysis.row, analysis.col), (7, 6))
        self.assertIn("双威胁", analysis.reason)

    def test_creates_double_closed_four_for_forced_win(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 4:7] = BLACK
        board[4:7, 7] = BLACK
        board[7, 3] = WHITE
        board[3, 7] = WHITE

        self.assertEqual(self.engine.best_move(board, BLACK), (7, 7))

    def test_blocks_opponent_open_four_creator(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, (4, 5, 7)] = WHITE
        board[3, 3] = BLACK

        analysis = self.engine.analyze(board, BLACK)

        self.assertEqual((analysis.row, analysis.col), (7, 6))
        self.assertIn("阻挡对方的双威胁", analysis.reason)

    def test_blocks_shared_point_of_multiple_forcing_threats(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[3, 4:7] = WHITE
        board[4:7, 3] = WHITE
        for row, col in ((8, 14), (10, 10), (10, 13), (12, 11), (13, 14), (14, 9)):
            board[row, col] = BLACK

        self.assertEqual(self.engine.best_move(board, BLACK), (3, 3))

    def test_uses_and_blocks_cross_double_three(self) -> None:
        attack = np.zeros((15, 15), dtype=np.int8)
        attack[7, 5:7] = BLACK
        attack[5:7, 7] = BLACK
        attack[4, 4] = WHITE
        attack[10, 10] = WHITE
        self.assertEqual(self.engine.best_move(attack, BLACK), (7, 7))

        defense = -attack
        self.assertEqual(self.engine.best_move(defense, BLACK), (7, 7))

    def test_move_score_only_counts_patterns_containing_move(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 4:7] = BLACK

        extension = self.engine.move_score(board, 7, 7, BLACK)
        unrelated = self.engine.move_score(board, 7, 9, BLACK)

        self.assertGreaterEqual(extension, 1_800_000)
        self.assertLess(unrelated, 9_000)

    def test_scores_broken_four_as_forcing_move(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, (4, 5, 7)] = BLACK

        self.assertGreaterEqual(self.engine.move_score(board, 7, 8, BLACK), 260_000)

    def test_detects_winner(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        for i in range(5):
            board[2 + i, 3 + i] = WHITE
        self.assertEqual(self.engine.winner(board), WHITE)

    def test_overline_wins_under_free_gomoku_rules(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[0, 0:6] = BLACK

        self.assertEqual(self.engine.winner(board), BLACK)

    def test_returns_only_legal_empty_candidates(self) -> None:
        rng = np.random.default_rng(101)
        board = np.zeros((15, 15), dtype=np.int8)
        cells = rng.choice(board.size, 20, replace=False)
        for index, cell in enumerate(cells):
            board.flat[cell] = BLACK if index % 2 == 0 else WHITE

        row, col = self.engine.best_move(board, BLACK)

        self.assertTrue(0 <= row < 15 and 0 <= col < 15)
        self.assertEqual(board[row, col], 0)

    def test_rejects_invalid_or_finished_boards(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.best_move(np.zeros((15, 14), dtype=np.int8), BLACK)

        invalid = np.zeros((15, 15), dtype=np.int8)
        invalid[0, 0] = 2
        with self.assertRaises(ValueError):
            self.engine.best_move(invalid, BLACK)

        finished = np.zeros((15, 15), dtype=np.int8)
        finished[2, 2:7] = BLACK
        with self.assertRaises(ValueError):
            self.engine.best_move(finished, WHITE)

        full = np.ones((3, 3), dtype=np.int8)
        full[::2, 1::2] = WHITE
        full[1::2, ::2] = WHITE
        with self.assertRaises(ValueError):
            self.engine.best_move(full, BLACK)

    def test_midgame_search_has_bounded_scoring_work(self) -> None:
        rng = np.random.default_rng(101)
        board = np.zeros((15, 15), dtype=np.int8)
        cells = rng.choice(board.size, 20, replace=False)
        for index, cell in enumerate(cells):
            board.flat[cell] = BLACK if index % 2 == 0 else WHITE

        calls = 0
        original_move_score = self.engine.move_score

        def counted_move_score(*args: object, **kwargs: object) -> int:
            nonlocal calls
            calls += 1
            return original_move_score(*args, **kwargs)

        self.engine.move_score = counted_move_score  # type: ignore[method-assign]
        started = perf_counter()
        self.engine.best_move(board, BLACK)
        elapsed = perf_counter() - started

        self.assertLess(calls, 40_000)
        self.assertLess(elapsed, 3.0)


if __name__ == "__main__":
    unittest.main()
