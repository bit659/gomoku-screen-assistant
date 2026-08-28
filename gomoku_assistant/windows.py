from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image, ImageGrab


user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GA_ROOT = 2

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)

user32.EnumWindows.argtypes = (EnumWindowsProc, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (
    wintypes.HWND,
    wintypes.LPWSTR,
    ctypes.c_int,
)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = (
    wintypes.HWND,
    wintypes.LPWSTR,
    ctypes.c_int,
)
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
)
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetActiveWindow.argtypes = (wintypes.HWND,)
user32.SetActiveWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND

dwmapi.DwmGetWindowAttribute.argtypes = (
    wintypes.HWND,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
)
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


Bounds = tuple[int, int, int, int]


class ClickCancelledError(OSError):
    """Raised before mouse-down when the caller requests an emergency stop."""


@dataclass(frozen=True, slots=True)
class _CaptureState:
    image_bounds: Bounds
    frame_bounds: Bounds
    client_bounds: Bounds
    process_id: int
    class_name: str


_capture_states: dict[int, _CaptureState] = {}
_capture_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """Current metadata for one top-level Windows window."""

    hwnd: int
    title: str
    class_name: str
    process_id: int
    bounds: Bounds
    client_bounds: Bounds
    visible: bool
    minimized: bool
    cloaked: bool

    @property
    def rect(self) -> Bounds:
        """Alias for the visible DWM frame bounds."""

        return self.bounds

    @property
    def origin(self) -> tuple[int, int]:
        """Screen position corresponding to pixel (0, 0) of a client capture."""

        return self.client_bounds[0], self.client_bounds[1]

    @property
    def size(self) -> tuple[int, int]:
        return _bounds_size(self.client_bounds)


def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def virtual_screen_bounds() -> Bounds:
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    if width <= 0 or height <= 0:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        left = top = 0
    return left, top, left + width, top + height


def _rect_tuple(rect: wintypes.RECT) -> Bounds:
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _bounds_size(bounds: Bounds) -> tuple[int, int]:
    return bounds[2] - bounds[0], bounds[3] - bounds[1]


def _valid_bounds(bounds: Bounds) -> bool:
    width, height = _bounds_size(bounds)
    return width > 0 and height > 0


def _point_in_bounds(x: int, y: int, bounds: Bounds) -> bool:
    return bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]


def _window_text(hwnd: int) -> str:
    length = max(0, int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd))))
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, len(buffer))
    return buffer.value[:copied] if copied else ""


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    copied = user32.GetClassNameW(wintypes.HWND(hwnd), buffer, len(buffer))
    return buffer.value[:copied] if copied else ""


def _raw_window_bounds(hwnd: int) -> Bounds:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise OSError(f"无法读取窗口矩形: HWND={hwnd}")
    return _rect_tuple(rect)


def _frame_bounds(hwnd: int, fallback: Bounds) -> Bounds:
    rect = wintypes.RECT()
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    bounds = _rect_tuple(rect)
    return bounds if result == 0 and _valid_bounds(bounds) else fallback


def _client_bounds(hwnd: int, fallback: Bounds) -> Bounds:
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return fallback
    top_left = wintypes.POINT(rect.left, rect.top)
    bottom_right = wintypes.POINT(rect.right, rect.bottom)
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(top_left)):
        return fallback
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(bottom_right)):
        return fallback
    bounds = (
        int(top_left.x),
        int(top_left.y),
        int(bottom_right.x),
        int(bottom_right.y),
    )
    return bounds if _valid_bounds(bounds) else fallback


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD()
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_CLOAKED,
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and bool(cloaked.value)


def get_window_info(hwnd: int) -> WindowInfo:
    """Return current screen-space metadata for ``hwnd``."""

    hwnd = int(hwnd)
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        raise ValueError(f"无效的窗口句柄: HWND={hwnd}")

    raw_bounds = _raw_window_bounds(hwnd)
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(process_id))
    return WindowInfo(
        hwnd=hwnd,
        title=_window_text(hwnd),
        class_name=_class_name(hwnd),
        process_id=int(process_id.value),
        bounds=_frame_bounds(hwnd, raw_bounds),
        client_bounds=_client_bounds(hwnd, raw_bounds),
        visible=bool(user32.IsWindowVisible(wintypes.HWND(hwnd))),
        minimized=bool(user32.IsIconic(wintypes.HWND(hwnd))),
        cloaked=_is_cloaked(hwnd),
    )


def enumerate_windows(
    title_contains: str | None = None,
    *,
    visible_only: bool = True,
    include_minimized: bool = True,
    include_cloaked: bool = False,
    include_untitled: bool = False,
) -> list[WindowInfo]:
    """Enumerate top-level windows in z-order (frontmost first)."""

    matches: list[WindowInfo] = []
    title_query = title_contains.casefold() if title_contains else None

    @EnumWindowsProc
    def collect(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> wintypes.BOOL:
        try:
            info = get_window_info(int(hwnd))
        except (OSError, ValueError):
            return True
        if visible_only and not info.visible:
            return True
        if not include_minimized and info.minimized:
            return True
        if not include_cloaked and info.cloaked:
            return True
        if not include_untitled and not info.title:
            return True
        if title_query is not None and title_query not in info.title.casefold():
            return True
        matches.append(info)
        return True

    if not user32.EnumWindows(collect, 0):
        raise OSError("枚举 Windows 顶层窗口失败")
    return matches


def list_candidate_windows() -> list[WindowInfo]:
    """Return visible, non-minimized, titled windows suitable for capture."""

    return enumerate_windows(include_minimized=False)


def list_windows(*args: object, **kwargs: object) -> list[WindowInfo]:
    """Compatibility-friendly alias for :func:`enumerate_windows`."""

    return enumerate_windows(*args, **kwargs)


def find_window(
    title: str,
    *,
    exact: bool = False,
    class_name: str | None = None,
) -> WindowInfo | None:
    """Find the frontmost visible, non-minimized window matching ``title``."""

    if not title:
        raise ValueError("窗口标题不能为空")
    title_key = title.casefold()
    class_key = class_name.casefold() if class_name else None
    for info in list_candidate_windows():
        candidate = info.title.casefold()
        if (candidate == title_key if exact else title_key in candidate) and (
            class_key is None or info.class_name.casefold() == class_key
        ):
            return info
    return None


def _capture_origin(
    image_size: tuple[int, int],
    info: WindowInfo,
    raw_bounds: Bounds,
) -> tuple[int, int]:
    candidates = (info.client_bounds, info.bounds, raw_bounds)
    for bounds in candidates:
        if _bounds_size(bounds) == image_size:
            return bounds[0], bounds[1]

    # Some native windows differ by one DWM edge pixel. Use the geometry whose
    # size is closest to the exact pixels Pillow returned.
    width, height = image_size
    closest = min(
        candidates,
        key=lambda bounds: abs(_bounds_size(bounds)[0] - width)
        + abs(_bounds_size(bounds)[1] - height),
    )
    return closest[0], closest[1]


def _image_is_uniform(image: Image.Image) -> bool:
    extrema = image.convert("RGB").getextrema()
    return max(high - low for low, high in extrema) <= 2


def capture_window(hwnd: int | WindowInfo) -> tuple[np.ndarray, tuple[int, int]]:
    """Capture a top-level window and return RGB pixels plus screen origin.

    Pillow's native HWND capture is used first, so the result remains usable
    when another window partially covers the target. Older Pillow builds fall
    back to a screen-space client-area grab.
    """

    hwnd_value = hwnd.hwnd if isinstance(hwnd, WindowInfo) else int(hwnd)
    info = get_window_info(hwnd_value)
    if not info.visible or info.cloaked:
        raise OSError(f"窗口不可见，无法可靠截图: {info.title or hwnd_value}")
    if info.minimized:
        raise OSError(f"窗口已最小化，无法可靠截图: {info.title or hwnd_value}")

    raw_bounds = _raw_window_bounds(hwnd_value)
    try:
        image = ImageGrab.grab(window=hwnd_value)
    except (OSError, TypeError, ValueError):
        image = ImageGrab.grab(bbox=info.client_bounds, all_screens=True)
        origin = info.origin
    else:
        origin = _capture_origin(image.size, info, raw_bounds)
        if _image_is_uniform(image):
            # Chromium and hardware-accelerated windows can make PrintWindow
            # return a correctly sized all-white/all-black frame. The target
            # is known to be visible here, so use its exact on-screen client
            # pixels instead of feeding an empty frame to board recognition.
            image = ImageGrab.grab(bbox=info.client_bounds, all_screens=True)
            origin = info.origin

    image_rgb = np.asarray(image.convert("RGB"))
    if image_rgb.ndim != 3 or image_rgb.shape[2] != 3 or image_rgb.size == 0:
        raise OSError(f"窗口截图为空: HWND={hwnd_value}")
    image_rgb = np.ascontiguousarray(image_rgb)
    image_bounds = (
        origin[0],
        origin[1],
        origin[0] + image_rgb.shape[1],
        origin[1] + image_rgb.shape[0],
    )
    with _capture_lock:
        _capture_states[hwnd_value] = _CaptureState(
            image_bounds=image_bounds,
            frame_bounds=info.bounds,
            client_bounds=info.client_bounds,
            process_id=info.process_id,
            class_name=info.class_name,
        )
    return image_rgb, origin


def capture_screen(
    hwnd: int | WindowInfo | None = None,
) -> tuple[np.ndarray, tuple[int, int]]:
    """Capture the virtual desktop, or one window when ``hwnd`` is supplied."""

    if hwnd is not None:
        return capture_window(hwnd)
    bounds = virtual_screen_bounds()
    image = ImageGrab.grab(bbox=bounds, all_screens=True)
    return np.ascontiguousarray(image.convert("RGB")), (bounds[0], bounds[1])


def click_screen(x: int, y: int) -> None:
    if not user32.SetCursorPos(int(x), int(y)):
        raise OSError("无法移动鼠标到目标位置")
    time.sleep(0.04)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.035)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _same_root_window(hit_hwnd: int, target_hwnd: int) -> bool:
    if not hit_hwnd:
        return False
    root = user32.GetAncestor(wintypes.HWND(hit_hwnd), GA_ROOT)
    return int(root or hit_hwnd) == target_hwnd


def _validate_safe_click(
    hwnd: int,
    x: int,
    y: int,
    capture_state: _CaptureState,
) -> WindowInfo:
    info = get_window_info(hwnd)
    if not info.visible or info.cloaked or info.minimized:
        raise OSError("目标窗口当前不可见或已最小化，已取消点击")
    if not _point_in_bounds(x, y, capture_state.image_bounds):
        raise OSError("点击坐标不在最近一次目标窗口截图内，已取消点击")
    if (
        info.process_id != capture_state.process_id
        or info.class_name != capture_state.class_name
    ):
        raise OSError("目标窗口句柄已指向其他窗口，已取消点击")
    if (
        info.bounds != capture_state.frame_bounds
        or info.client_bounds != capture_state.client_bounds
    ):
        raise OSError("目标窗口在识别后发生移动或缩放，已取消点击")
    return info


def _activate_for_click(hwnd: int) -> bool:
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    current_thread = int(ctypes.windll.kernel32.GetCurrentThreadId())
    target_thread = int(user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None))
    foreground = int(user32.GetForegroundWindow() or 0)
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(wintypes.HWND(foreground), None)) if foreground else 0
    )
    attached: list[int] = []
    try:
        for thread_id in {target_thread, foreground_thread}:
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetActiveWindow(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current_thread, thread_id, False)
    time.sleep(0.10)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def click_window(
    hwnd: int | WindowInfo,
    x: int,
    y: int,
    *,
    clicks: int = 1,
    interval: float = 0.16,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Safely click screen coordinates belonging to a captured target window.

    A successful :func:`capture_window` call for the same HWND is required.
    The click fails closed if the target moved, disappeared, cannot become the
    foreground window, or another top-level window covers the point.
    """

    if clicks not in (1, 2):
        raise ValueError("窗口点击次数仅支持 1 或 2")
    hwnd_value = hwnd.hwnd if isinstance(hwnd, WindowInfo) else int(hwnd)
    x, y = int(x), int(y)
    with _capture_lock:
        capture_state = _capture_states.get(hwnd_value)
    if capture_state is None:
        raise OSError("目标窗口尚未截图识别，已取消点击")

    def check_cancelled() -> None:
        if cancelled is not None and cancelled():
            raise ClickCancelledError("F8 急停已触发，点击已取消")

    check_cancelled()
    _validate_safe_click(hwnd_value, x, y, capture_state)
    if not _activate_for_click(hwnd_value):
        raise OSError("目标窗口未成为前台窗口，已取消点击")
    check_cancelled()
    _validate_safe_click(hwnd_value, x, y, capture_state)

    hit = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not _same_root_window(int(hit or 0), hwnd_value):
        raise OSError("点击位置被其他窗口覆盖，已取消点击")
    if not user32.SetCursorPos(x, y):
        raise OSError("无法移动鼠标到目标位置")
    time.sleep(0.04)
    check_cancelled()

    _validate_safe_click(hwnd_value, x, y, capture_state)
    hit = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not _same_root_window(int(hit or 0), hwnd_value):
        raise OSError("点击位置在执行前被其他窗口覆盖，已取消点击")
    for click_index in range(clicks):
        check_cancelled()
        _validate_safe_click(hwnd_value, x, y, capture_state)
        hit = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not _same_root_window(int(hit or 0), hwnd_value):
            raise OSError("点击位置在执行前被其他窗口覆盖，已取消点击")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.045)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        if click_index + 1 < clicks:
            check_cancelled()
            time.sleep(max(0.08, min(float(interval), 0.8)))
            check_cancelled()


def key_is_down(virtual_key: int) -> bool:
    # Include the low bit so a quick press/release during a blocking native call
    # is still observed on the next check.
    return bool(user32.GetAsyncKeyState(virtual_key) & 0x8001)


def exclude_window_from_capture(hwnd: int) -> bool:
    try:
        return bool(user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), 0x11))
    except (AttributeError, OSError):
        return False
