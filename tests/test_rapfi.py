import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from gomoku_assistant.engine import BLACK, WHITE
from gomoku_assistant.rapfi import RapfiEngine, RapfiError


class RapfiProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RapfiEngine(think_time_ms=200, threads=1, memory_mb=96)

    def tearDown(self) -> None:
        self.engine.close()

    def test_official_runtime_and_license_are_bundled(self) -> None:
        engine_dir = self.engine.engine_dir
        expected_hashes = {
            "pbrain-rapfi-windows-avx2.exe": "1E02497433FB765626F954C895B0B92E057AC12A76C76318ABE74D4050914E8E",
            "pbrain-rapfi-windows-sse.exe": "DC98082D74E862547FD7F45130399782F21F6023AE15676A2AD471ECD1DACA4C",
            "mix9svqfreestyle_bsmix.bin.lz4": "6BC0D1B0FF8E1D857F7F412923CD458F38F1A435087C91AE71FEC3676C23EF62",
            "model210901.bin": "2FA08CB15CF240D9533548C3E011B03D46809F6858362E97EE873D0632AA2015",
            "rapfi-source-250615.zip": "A2A410DE094F5813808CB99AC278C0180A0F26694E5F015A87FAE082B22D2EFC",
        }
        for name, expected_hash in expected_hashes.items():
            path = engine_dir / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest().upper(), expected_hash)

        for name in ("config.toml", "COPYING.txt", "NETWORK_WEIGHTS_CC0.txt"):
            self.assertTrue((engine_dir / name).is_file(), name)

    def test_instruction_set_fallback_is_explicit(self) -> None:
        with (
            patch.object(RapfiEngine, "_has_avx2", return_value=False),
            patch.object(RapfiEngine, "_has_sse41", return_value=True),
        ):
            self.assertEqual(
                self.engine._resolve_executable().name,
                "pbrain-rapfi-windows-sse.exe",
            )

        with (
            patch.object(RapfiEngine, "_has_avx2", return_value=False),
            patch.object(RapfiEngine, "_has_sse41", return_value=False),
            self.assertRaises(RapfiError),
        ):
            self.engine._resolve_executable()

    def test_board_protocol_uses_xy_and_engine_perspective(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[2, 11] = BLACK
        board[8, 4] = WHITE

        self.assertEqual(
            RapfiEngine._board_lines(board, BLACK),
            ["BOARD", "11,2,1", "4,8,2", "DONE"],
        )

        board[5, 9] = BLACK
        self.assertEqual(
            RapfiEngine._board_lines(board, WHITE),
            ["BOARD", "11,2,2", "4,8,1", "9,5,2", "DONE"],
        )

    def test_board_protocol_rejects_wrong_side_to_move(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 7] = BLACK

        with self.assertRaises(ValueError):
            RapfiEngine._board_lines(board, BLACK)

    def test_illegal_engine_coordinates_are_rejected(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[2, 3] = BLACK

        with self.assertRaises(RapfiError):
            RapfiEngine._validate_move(board, 15, 0)
        with self.assertRaises(RapfiError):
            RapfiEngine._validate_move(board, 3, 2)

    def test_real_engine_opens_in_center(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)

        self.assertEqual(self.engine.best_move(board, BLACK), (7, 7))

    def test_real_engine_supports_19_by_19(self) -> None:
        board = np.zeros((19, 19), dtype=np.int8)

        self.assertEqual(self.engine.best_move(board, BLACK), (9, 9))

    def test_real_engine_finishes_an_immediate_win(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 3:7] = BLACK
        board[6, 7] = WHITE
        board[8, 7:10] = WHITE

        self.assertIn(self.engine.best_move(board, BLACK), ((7, 2), (7, 7)))

    def test_real_engine_takes_own_win_when_both_sides_threaten_five(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 3:7] = BLACK
        board[3, 3:7] = WHITE

        self.assertIn(self.engine.best_move(board, BLACK), ((7, 2), (7, 7)))

    def test_real_engine_takes_white_win_when_both_sides_threaten_five(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 3:7] = WHITE
        board[3, 3:7] = BLACK
        board[10, 10] = BLACK

        self.assertIn(self.engine.best_move(board, WHITE), ((7, 2), (7, 7)))

    def test_real_engine_uses_requested_side_on_asymmetric_position(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[(7, 8, 9), (7, 7, 7)] = BLACK
        board[(6, 5, 4), (6, 6, 6)] = WHITE

        self.assertEqual(self.engine.best_move(board, BLACK), (6, 7))

    def test_real_engine_returns_legal_move_as_white(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        board[7, 7] = BLACK

        row, col = self.engine.best_move(board, WHITE)

        self.assertEqual(board[row, col], 0)
        self.assertTrue(0 <= row < 15 and 0 <= col < 15)

    def test_real_engine_process_is_reused_across_turns(self) -> None:
        board = np.zeros((15, 15), dtype=np.int8)
        self.engine.best_move(board, BLACK)
        assert self.engine._process is not None
        first_pid = self.engine._process.pid
        board[7, 7] = BLACK

        row, col = self.engine.best_move(board, WHITE)

        self.assertEqual(self.engine._process.pid, first_pid)
        self.assertIsNone(self.engine._process.poll())
        self.assertEqual(board[row, col], 0)

    def test_missing_runtime_fails_instead_of_using_weak_fallback(self) -> None:
        missing = Path(__file__).resolve().parent / "does-not-exist"
        engine = RapfiEngine(missing, think_time_ms=100)
        try:
            with self.assertRaises(RapfiError):
                engine.best_move(np.zeros((15, 15), dtype=np.int8), BLACK)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
