import tkinter as tk
import threading
from time import perf_counter
import unittest
from unittest.mock import patch

import numpy as np

from gomoku_assistant.autoplay import AutoPlayState
from gomoku_assistant.detector import locate_board
from gomoku_assistant.engine import GomokuEngine
from gomoku_assistant.models import BoardGeometry
from gomoku_assistant.ui import GomokuApp
from gomoku_assistant.windows import ClickCancelledError

from tests.test_detector import synthetic_screen


class GateEngine(GomokuEngine):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze(self, board: np.ndarray, player: int):
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test gate timed out")
        return super().analyze(board, player)

    def abort(self) -> None:
        self.release.set()

    def close(self) -> None:
        self.release.set()


class UiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = GomokuApp(self.root)
        self.app.engine = GomokuEngine()
        self.app._target_hwnd = 1234
        lines = np.linspace(50, 470, 15)
        self.geometry = BoardGeometry(lines, lines.copy(), 0.99, "manual")
        self.app._locked_geometry = self.geometry
        self.app._locked_capture_shape = (540, 540)
        self.image = np.full((540, 540, 3), 180, dtype=np.uint8)
        self.board = np.zeros((15, 15), dtype=np.int8)
        self.confidence = np.ones((15, 15), dtype=np.float32)

    def _liveness_patch(self):
        # Most integration tests mock stone recognition and intentionally use
        # a flat placeholder image. Grid liveness itself is covered with real
        # detector frames below, so these tests opt into a live mocked board.
        return patch("gomoku_assistant.ui.grid_liveness", return_value=1.0)

    def tearDown(self) -> None:
        try:
            self.app._stop_auto("test cleanup")
            self.app._engine_executor.shutdown(wait=True, cancel_futures=True)
            self.root.destroy()
        except tk.TclError:
            pass

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_manual_geometry_survives_execute_once(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)
            self.app.click_allowed.set(True)

            self.assertTrue(self.app.execute_once())
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        self.assertEqual(capture_window.call_count, 2)
        self.assertEqual(read_stones.call_count, 2)
        # 单击交点 + 黄色确认按钮 = 每次落子两次 click_window，均为单击。
        self.assertEqual(click_window.call_count, 2)
        for call in click_window.call_args_list:
            self.assertEqual(call.kwargs["clicks"], 1)

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_manual_execute_confirms_previous_click_before_next_move(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        replied = self.board.copy()
        replied[7, 7] = 1
        replied[6, 7] = -1
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.side_effect = [
                (self.board, self.confidence),
                (self.board, self.confidence),
                (replied, self.confidence),
                (replied, self.confidence),
            ]
            self.app.click_allowed.set(True)

            self.assertTrue(self.app.execute_once())
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()
            self.assertEqual(self.app.auto_state.pending_move, (7, 7))

            self.app._last_click_at = 0.0
            self.assertTrue(self.app.execute_once())
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        # 两次 execute_once 各落一子，单击+确认模式每次两次 click_window。
        self.assertEqual(click_window.call_count, 4)
        self.assertNotEqual(self.app.auto_state.pending_move, (7, 7))

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_auto_tick_consumes_one_atomic_scan(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)
            self.app.auto_state = AutoPlayState(stable_reads=1)
            self.app.click_allowed.set(True)
            self.app.auto_enabled.set(True)

            self.app._auto_tick()
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        self.assertEqual(capture_window.call_count, 2)
        self.assertEqual(read_stones.call_count, 2)
        # 单击 + 确认按钮 = 每次落子两次 click_window。
        self.assertEqual(click_window.call_count, 2)
        self.assertEqual(self.app.auto_state.pending_move, (7, 7))

    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.capture_window")
    def test_auto_rejects_same_size_uniform_frame_without_click(
        self, capture_window, click_window
    ) -> None:
        board_image = synthetic_screen()
        geometry = locate_board(board_image, 15)
        self.app._locked_geometry = geometry
        self.app._locked_capture_shape = board_image.shape[:2]
        capture_window.return_value = (
            np.full_like(board_image, (210, 170, 110)),
            (100, 200),
        )
        self.app.auto_state = AutoPlayState(stable_reads=1)
        self.app.click_allowed.set(True)
        self.app.auto_enabled.set(True)

        self.app._auto_tick()

        click_window.assert_not_called()
        self.assertIsNone(self.app._engine_future)
        self.assertEqual(self.app._scan_failures, 1)
        self.assertTrue(self.app.auto_enabled.get())

    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.capture_window")
    def test_execute_rejects_translated_locked_board_without_click(
        self, capture_window, click_window
    ) -> None:
        board_image = synthetic_screen()
        geometry = locate_board(board_image, 15)
        translated = np.roll(board_image, shift=(8, 8), axis=(0, 1))
        translated[:8, :] = (239, 241, 244)
        translated[:, :8] = (239, 241, 244)
        self.app._locked_geometry = geometry
        self.app._locked_capture_shape = board_image.shape[:2]
        capture_window.return_value = (translated, (100, 200))
        self.app.click_allowed.set(True)

        self.assertFalse(self.app.execute_once())

        click_window.assert_not_called()
        self.assertIsNone(self.app._engine_future)
        self.assertIn("网格", self.app.status_text.get())

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_execute_discards_result_if_board_changes_while_thinking(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        changed = self.board.copy()
        changed[7, 7] = 1
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.side_effect = [
                (self.board, self.confidence),
                (changed, self.confidence),
            ]
            self.app.click_allowed.set(True)

            self.assertTrue(self.app.execute_once())
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        click_window.assert_not_called()
        self.assertIn("已丢弃旧落点", self.app.status_text.get())

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_f8_style_revoke_prevents_click_during_background_search(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)
            gate = GateEngine()
            self.app.engine = gate
            self.app.click_allowed.set(True)

            self.assertTrue(self.app.execute_once())
            self.assertTrue(gate.started.wait(timeout=1))
            self.app._stop_auto("F8 test", revoke_click=True)
            gate.release.set()
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        click_window.assert_not_called()
        self.assertFalse(self.app.click_allowed.get())

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window", side_effect=ClickCancelledError("F8 test"))
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_emergency_stop_inside_double_click_revokes_permissions(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)
            self.app.click_allowed.set(True)
            self.app.auto_enabled.set(True)

            self.assertTrue(self.app.execute_once())
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        click_window.assert_called_once()
        self.assertFalse(self.app.click_allowed.get())
        self.assertFalse(self.app.auto_enabled.get())

    @patch("gomoku_assistant.ui.locate_board")
    @patch("gomoku_assistant.ui.click_window")
    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_auto_resumes_if_enabled_during_manual_suggestion(
        self, capture_window, read_stones, click_window, locate_board
    ) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)

            self.app.calculate_move()
            self.app.click_allowed.set(True)
            self.app.auto_enabled.set(True)
            self.app._auto_job = None
            assert self.app._engine_future is not None
            self.app._engine_future.result(timeout=3)
            self.app._poll_engine_result()

        locate_board.assert_not_called()
        click_window.assert_not_called()
        self.assertIsNotNone(self.app._auto_job)

    @patch("gomoku_assistant.ui.read_stones")
    @patch("gomoku_assistant.ui.capture_window")
    def test_close_does_not_wait_for_background_search(self, capture_window, read_stones) -> None:
        with self._liveness_patch():
            capture_window.return_value = (self.image, (100, 200))
            read_stones.return_value = (self.board, self.confidence)
            gate = GateEngine()
            self.app.engine = gate

            self.app.calculate_move()
            self.assertTrue(gate.started.wait(timeout=1))
            started = perf_counter()
            self.app.close()

        self.assertLess(perf_counter() - started, 0.5)
        gate.release.set()


if __name__ == "__main__":
    unittest.main()
