# -*- coding: utf-8 -*-
"""把文件直接拖进 Tk 窗口（Windows 拖放，纯 ctypes 实现）。

为什么不用 tkinterdnd2：它依赖 tkdnd 的 tcl 扩展库，PyInstaller 单文件
打包时要额外把 tkdnd 数据目录塞进包里，换机器还容易找不到 tcl 脚本。
这里直接接 Win32 的 WM_DROPFILES 消息，打包前后行为一致、零额外依赖。

关键约束：窗口过程（WndProc）运行在 C 的消息分发里，此时 Tk 正在
update/等待消息，GIL 状态不安全。因此在 WndProc 里只做两件事——
读路径、把路径塞进线程安全队列，绝不调用任何 Tk API；真正的回调由
Tk 的 after() 轮询在主线程正常上下文里执行。

用法：
    ok = enable_file_drop(root, on_drop_paths)   # on_drop_paths(list[str])
    disable_file_drop(root)                      # 收尾/销毁前可选
"""
from __future__ import annotations

import ctypes
import queue
import sys
from ctypes import wintypes

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

_LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

# 窗口过程：LRESULT CALLBACK(HWND, UINT, WPARAM, LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_set_window_long = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
_set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_set_window_long.restype = ctypes.c_void_p

_call_window_proc = _user32.CallWindowProcW
_call_window_proc.argtypes = [
    ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
_call_window_proc.restype = _LRESULT

_get_parent = _user32.GetParent
_get_parent.argtypes = [wintypes.HWND]
_get_parent.restype = wintypes.HWND

_drag_accept_files = _shell32.DragAcceptFiles
_drag_accept_files.argtypes = [wintypes.HWND, wintypes.BOOL]
_drag_accept_files.restype = None

_drag_query_file = _shell32.DragQueryFileW
_drag_query_file.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
_drag_query_file.restype = wintypes.UINT

_drag_finish = _shell32.DragFinish
_drag_finish.argtypes = [wintypes.HANDLE]
_drag_finish.restype = None

# hwnd -> state：必须长期持有回调对象，否则被 GC 后窗口消息会打进野指针
_TARGETS: dict[int, dict] = {}


def window_handle(root) -> int | None:
    """取 Tk 顶层窗口的 HWND（拖放消息只发给顶层窗口）。"""
    try:
        frame = root.wm_frame()
        if frame:
            value = int(str(frame), 16)
            if value:
                return value
    except Exception:  # noqa: BLE001 - 取不到就回退
        pass
    try:
        own = int(root.winfo_id())
        parent = _get_parent(wintypes.HWND(own))
        return int(parent) if parent else own
    except Exception:  # noqa: BLE001
        return None


def _query_paths(hdrop) -> list[str]:
    """从 HDROP 里读出所有被拖入的文件路径（宽字符接口，中文原样）。"""
    handle = wintypes.HANDLE(hdrop)
    count = int(_drag_query_file(handle, 0xFFFFFFFF, None, 0))
    if count <= 0:
        return []
    buf = ctypes.create_unicode_buffer(32768)
    paths = []
    for index in range(count):
        length = int(_drag_query_file(handle, index, buf, len(buf)))
        if length and buf.value:
            paths.append(buf.value)
    return paths


def enable_file_drop(root, on_drop, interval_ms: int = 80) -> bool:
    """让窗口接受文件拖入。成功返回 True；非 Windows 或取不到句柄返回 False。

    Tk 首次 update 时才会创建真正的顶层 wrapper，句柄会变，所以注册不能只做
    一次：轮询里每次比对当前句柄，变了就撤旧的、挂新的（否则消息打到一个
    没有拖放注册的窗口上，表现为“拖进去没反应”）。
    """
    if not sys.platform.startswith("win"):
        return False
    hwnd = window_handle(root)
    if not hwnd:
        return False

    pending: queue.Queue[list[str]] = queue.Queue()
    state: dict = {"root": root, "on_drop": on_drop, "hwnd": None,
                   "pending": pending, "after_id": None, "proc": None, "old": None}

    def _proc(hwnd_arg, msg, wparam, lparam):  # C 消息上下文：只读路径 + 入队
        if msg == WM_DROPFILES:
            try:
                paths = _query_paths(wparam)
            except Exception:  # noqa: BLE001
                paths = []
            try:
                _drag_finish(wintypes.HANDLE(wparam))
            except Exception:  # noqa: BLE001
                pass
            if paths:
                pending.put(paths)
            return 0
        return _call_window_proc(state["old"], hwnd_arg, msg, wparam, lparam)

    def _attach(target: int) -> bool:
        proc = _WNDPROC(_proc)
        old = _set_window_long(wintypes.HWND(target), GWLP_WNDPROC,
                               ctypes.cast(proc, ctypes.c_void_p))
        if not old:
            return False
        state["proc"] = proc
        state["old"] = old
        state["hwnd"] = target
        _TARGETS[target] = state
        _drag_accept_files(wintypes.HWND(target), True)
        return True

    def _detach() -> None:
        old_hwnd = state.get("hwnd")
        if not old_hwnd:
            return
        try:
            _drag_accept_files(wintypes.HWND(old_hwnd), False)
            if state.get("old"):
                _set_window_long(wintypes.HWND(old_hwnd), GWLP_WNDPROC, state["old"])
        except Exception:  # noqa: BLE001
            pass
        _TARGETS.pop(old_hwnd, None)
        state["hwnd"] = None

    def _poll():  # Tk 主线程正常上下文：跟随窗口句柄 + 派发回调
        current = window_handle(root)
        if current and current != state.get("hwnd"):
            _detach()
            _attach(current)
        drained: list[str] = []
        while True:
            try:
                drained.extend(pending.get_nowait())
            except queue.Empty:
                break
        if drained:
            try:
                on_drop(drained)
            except Exception:  # noqa: BLE001 - 导入失败不应打死轮询
                pass
        try:
            state["after_id"] = root.after(interval_ms, _poll)
        except Exception:  # noqa: BLE001 - 窗口已销毁
            state["after_id"] = None

    if not _attach(hwnd):
        return False
    state["after_id"] = root.after(interval_ms, _poll)
    return True


def disable_file_drop(root) -> bool:
    """撤掉拖放注册、取消轮询并恢复原窗口过程。"""
    hwnd = window_handle(root)
    state = next((s for s in _TARGETS.values() if s["root"] is root), None)
    if state is None:
        state = _TARGETS.get(hwnd) if hwnd else None
    if not state:
        return False
    try:
        if state.get("after_id") is not None:
            state["root"].after_cancel(state["after_id"])
    except Exception:  # noqa: BLE001
        pass
    state["after_id"] = None
    old_hwnd = state.get("hwnd")
    if not old_hwnd:
        return False
    try:
        _drag_accept_files(wintypes.HWND(old_hwnd), False)
        if state.get("old"):
            _set_window_long(wintypes.HWND(old_hwnd), GWLP_WNDPROC, state["old"])
    except Exception:  # noqa: BLE001
        return False
    _TARGETS.pop(old_hwnd, None)
    state["hwnd"] = None
    return True
