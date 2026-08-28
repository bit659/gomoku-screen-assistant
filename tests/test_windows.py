import unittest
from unittest.mock import Mock, call, patch

from gomoku_assistant import windows


class SafeClickTests(unittest.TestCase):
    def test_emergency_stop_between_clicks_prevents_confirmation_click(self) -> None:
        hwnd = 1234
        capture_state = windows._CaptureState(
            image_bounds=(0, 0, 200, 200),
            frame_bounds=(0, 0, 200, 200),
            client_bounds=(0, 0, 200, 200),
            process_id=99,
            class_name="test",
        )
        fake_user32 = Mock()
        fake_user32.WindowFromPoint.return_value = hwnd
        fake_user32.SetCursorPos.return_value = True

        def cancelled() -> bool:
            return fake_user32.mouse_event.call_count >= 2

        with (
            patch.dict(windows._capture_states, {hwnd: capture_state}, clear=True),
            patch.object(windows, "user32", fake_user32),
            patch.object(windows, "_validate_safe_click"),
            patch.object(windows, "_activate_for_click", return_value=True),
            patch.object(windows, "_same_root_window", return_value=True),
            patch.object(windows.time, "sleep", return_value=None),
        ):
            with self.assertRaises(windows.ClickCancelledError):
                windows.click_window(hwnd, 80, 90, clicks=2, cancelled=cancelled)

        self.assertEqual(
            fake_user32.mouse_event.call_args_list,
            [call(0x0002, 0, 0, 0, 0), call(0x0004, 0, 0, 0, 0)],
        )


if __name__ == "__main__":
    unittest.main()
