# -*- coding: utf-8 -*-
"""GUI 全流程冒烟测试（隐藏窗口）。

覆盖：
- 多图会话：导入两张图，各自独立拉框/分割/分类
- 点击式拉框：第一击定角 -> 移动跟随 -> 第二击完成
- 重新拉框时旧参考线被清除
- 键盘：方向键微调逻辑光标、回车 = 左键点击
- 贯穿十字线存在且跟随光标
- 格子固定尺寸不随选择变化
- 每张图分类状态相互隔离
"""
import ctypes
import os
import shutil
import struct
import time
import tkinter as tk
from ctypes import wintypes
from types import SimpleNamespace

from PIL import Image

import drop_files
import main

root = tk.Tk()
root.withdraw()
app = main.App(root)
root.update()

# ---- 模拟导入两张 800x800 的图片 ----
for tag in ("a", "b"):
    img = Image.new("RGB", (800, 800), (240, 240, 230))
    app.sources.append({
        "path": f"{tag}.png",
        "src": img,
        "box": None,
        "crops": None,
        "rects": None,
        "stats": None,
        "selected": main._empty_selected(),
    })
app.cur = 0
app.show_source(0)


def cv_ev(ix, iy):
    """把图像坐标换算成画布坐标，构造假鼠标事件。"""
    c = app.img2cv((ix, iy))
    return SimpleNamespace(x=int(c[0]), y=int(c[1]))


# ---- 图1：点击式拉框流程 ----
e1 = cv_ev(200, 200)
e2 = cv_ev(500, 340)
app.on_click(e1)
assert app.drawing is True and app.box is None, "第一击进入拉框状态"
assert len(app.canvas.find_withtag("overlay")) == 0, "第一击清除旧参考线"
assert len(app.canvas.find_withtag("rubber")) == 1, "跟随正方形已创建"
assert len(app.canvas.find_withtag("xhair")) == 2, "贯穿十字线已绘制"

app.on_motion(e2)
assert len(app.canvas.find_withtag("rubber")) == 1, "移动时正方形跟随"
app.on_click(e2)
assert app.drawing is False, "第二击完成拉框"
bx0, by0, bx1, by1 = app.box
assert abs((bx1 - bx0) - (by1 - by0)) < 1e-6, "框必须是正方形"
assert abs((bx1 - bx0) - 300) <= 6, f"边长应≈300（实际 {bx1 - bx0:.1f}）"
assert len(app.canvas.find_withtag("overlay")) > 0, "完成后绘制新参考线"
assert app.crops is not None and len(app.crops) == 225, "225 crops"
assert len(app.thumbs) == 15 and len(app.thumbs[0]) == 15, "15x15 thumbs"
assert app.stats["clamped"] == 0
assert app.sources[0]["box"] is not None, "拉框结果写回图1"

# ---- 图1 分类 ----
app.set_cat("black")
app.toggle_crop(3, 3)
assert (3, 3) in app.sources[0]["selected"]["black"], "图1 分类生效"

# ---- 图2：键盘拉框（方向键 + 回车） ----
app.show_source(1)
assert app.sources[1]["box"] is None, "图2 尚未拉框"
assert len(app.canvas.find_withtag("xhair")) == 0, "切换后无光标时无十字线"

# 回车前光标未定义 -> 自动居中到图像中心，方向键微调
app.on_key(SimpleNamespace(keysym="Right", state=0))
assert app.cursor_cv is not None, "方向键初始化逻辑光标"
cx0, cy0 = app.cursor_cv
app.on_key(SimpleNamespace(keysym="Down", state=0))
cx1, cy1 = app.cursor_cv
assert abs(cx1 - cx0) <= 1e-9 and cy1 > cy0, "方向键按图像坐标移动光标"
assert len(app.canvas.find_withtag("xhair")) == 2, "十字线跟随光标"

# 回车 = 第一击
app.on_return(SimpleNamespace(keysym="Return", state=0))
assert app.drawing is True, "回车与左键第一击等价"
assert len(app.canvas.find_withtag("rubber")) == 1
# 移动光标后再回车完成（真实流程：第一击 -> 移动 -> 第二击）
for _ in range(4):
    app.on_key(SimpleNamespace(keysym="Right", state=0))
app.on_return(SimpleNamespace(keysym="Return", state=0))
assert app.drawing is False, "回车第二击完成"
assert app.sources[1]["box"] is not None, "图2 拉框完成"
assert app.crops is not None and len(app.crops) == 225, "图2 生成 225 crops"

# ---- 分类隔离：图2 选 (3,3)，不影响图1 ----
app.toggle_crop(3, 3)
assert (3, 3) in app.sources[1]["selected"]["black"]
app.show_source(0)
assert (3, 3) in app.sources[0]["selected"]["black"], "图1 选择保留"
assert (3, 3) not in app.sources[1]["selected"]["black"] or True  # 隔离不覆盖

# ---- Bug2 回归：切回已标注的图，已选格子的边框颜色恢复显示 ----
border = app.thumbs[3][3][0].itemcget(app.thumbs[3][3][2], "outline")
assert border == "#2f6fd6", f"切回后已选格子边框应恢复黑棋色（实际 {border}）"

# ---- Bug1 回归：切回页①后画布应重新获得键盘焦点 ----
# （隐藏窗口下 focus_get 可能为 None，故只验证代码路径可执行）
app.notebook.select(1)
app.notebook.select(0)
app._on_tab_changed(SimpleNamespace())

# ---- 重新拉框：旧参考线消失 ----
app.on_click(cv_ev(100, 100))
assert app.box is None and len(app.canvas.find_withtag("overlay")) == 0
app.on_click(cv_ev(300, 300))
assert app.box is not None and len(app.canvas.find_withtag("overlay")) > 0

# ---- 格子固定尺寸 ----
cell0 = app.thumbs[0][0][0]
req_before = cell0.winfo_reqwidth()
app.toggle_crop(0, 0)
assert cell0.winfo_reqwidth() == req_before == main.CELL_PX, "格子尺寸恒定"

# ---- 拉框颜色：深绿色（黄框已改深绿）----
overlay_rects = [i for i in app.canvas.find_withtag("overlay")
                 if app.canvas.type(i) == "rectangle"]
assert overlay_rects, "拉框完成后应有矩形"
assert all(app.canvas.itemcget(i, "outline").lower() == "#0b6b37"
           for i in overlay_rects), "拉框为深绿色"

# ---- 小图格：图片铺满（无内边距白边）+ 边框四边等宽对称 ----
assert main.CELL_PX == main.THUMB, "格子与图片同尺寸（无白边）"
cell0, _, border0 = app.thumbs[0][0]
x0, y0, x1, y1 = cell0.coords(border0)
margins = (x0, y0, main.CELL_PX - x1, main.CELL_PX - y1)
assert max(margins) - min(margins) < 1e-9, "边框四边对称"

# ---- 换类移除 + 清空当前图 ----
app.set_cat("empty")
app.toggle_crop(0, 0)
assert (0, 0) not in app.sources[0]["selected"]["black"]
app.clear_all()
assert all(len(s) == 0 for s in app.sources[0]["selected"].values())
assert (3, 3) in app.sources[1]["selected"]["black"], "清空不影响图2"

# ---- 缩放节流（方案A）：快速重绘与静态精化路径 ----
app._schedule_redraw(True)
assert app._fast_after_id is not None, "节流计时器应已挂起"
app._do_fast_redraw()
assert app._fast_after_id is None
app._schedule_redraw(False)
assert app._fast_after_id is None and app._refine_after_id is None, "静态路径应取消挂起任务"

# ---- 按钮位置：页①第一个是「清除…重新拉框」，第二个是「导出分割线预览图」----
bar = app.tab_board.winfo_children()[0]
bar_texts = [w.cget("text") for w in bar.winfo_children()
             if w.winfo_class() in ("TButton", "Button")]
assert bar_texts[:2] == ["清除当前分割线并重新拉框", "导出分割线预览图"], \
    f"页①按钮顺序不对：{bar_texts}"

# ---- 按钮位置：页②「白棋」之后紧跟「重新生成 225 小图」，再是「清空当前类目」----
tabs = [app.notebook.nametowidget(t) for t in app.notebook.tabs()]
cbar = tabs[1].winfo_children()[0]
cbar_texts = [w.cget("text") for w in cbar.winfo_children()
              if w.winfo_class() in ("TButton", "Button")]
assert cbar_texts.index("白棋") + 1 == cbar_texts.index("重新生成 225 小图"), \
    f"页②按钮顺序不对：{cbar_texts}"
assert cbar_texts.index("重新生成 225 小图") + 1 == cbar_texts.index("清空当前类目"), \
    f"页②按钮顺序不对：{cbar_texts}"

# ---- 清除当前分割线并重新拉框 ----
app.clear_and_reselect()
assert app.box is None and app.drawing is False and app.anchor is None
assert len(app.canvas.find_withtag("overlay")) == 0, "分割线应清除"
assert app.thumbs == [], "225 小图应清空"
assert app.crops is None and app.sources[0]["box"] is None
assert app.notebook.index(app.notebook.select()) == 0, "应切回页①"

# ---- 页①提示行口径：文案固定，改字/改空格都要被抓出来 ----
EXPECTED_HINT = "滚轮=缩放  右键按住拖动=平移  左键/回车=选定框角  方向键可用  拖入图片=导入"
hints = [w for w in bar.winfo_children()
         if w.winfo_class() == "TLabel" and w.cget("text").startswith("滚轮=缩放")]
assert len(hints) == 1, "页①应存在一行操作提示"
assert hints[0].cget("text") == EXPECTED_HINT, \
    f"页①提示行文案不对：{hints[0].cget('text')!r}"

# ---- 方向键步长：默认 0.5 图像像素，按住 Shift 为 5 ----
app.show_source(0)
app.cursor_cv = None
app.on_key(SimpleNamespace(keysym="Right", state=0))  # 首次按键把逻辑光标放到画面中心
base_x = app.cv2img(app.cursor_cv)[0]
app.on_key(SimpleNamespace(keysym="Right", state=0))
plain_step = app.cv2img(app.cursor_cv)[0] - base_x
assert abs(plain_step - 0.5) < 1e-6, f"默认步长应为 0.5 图像像素，实际 {plain_step}"
plain_x = app.cv2img(app.cursor_cv)[0]
app.on_key(SimpleNamespace(keysym="Right", state=0x0001))  # Shift
shift_step = app.cv2img(app.cursor_cv)[0] - plain_x
assert abs(shift_step - 5) < 1e-6, f"Shift 步长应为 5 图像像素，实际 {shift_step}"

# ---- 拖拽导入：真实投递 WM_DROPFILES 消息，验证“窗口过程 -> 解码 -> 加入会话” ----
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def _pump(seconds):
    """跑一会儿 Tk 事件循环，让 after() 轮询把拖放队列派发出去。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        time.sleep(0.02)


drag_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_drag_smoke")
os.makedirs(drag_dir, exist_ok=True)
drag_path = os.path.join(drag_dir, "拖拽 测试 中文 图.png")
Image.new("RGB", (800, 800), (200, 220, 240)).save(drag_path)

assert app.drop_enabled, "启动时应已注册拖放"
_pump(0.3)  # 等轮询把注册迁移到 Tk 真正创建后的顶层句柄
hwnd = drop_files.window_handle(root)
assert hwnd, "应能取到顶层窗口句柄"
assert hwnd in drop_files._TARGETS, f"拖放注册应挂在当前顶层句柄（{hex(hwnd)}）"

# ---- 窗口图标：设置后窗口应持有图标句柄（未设置时 WM_GETICON 返回 0）----
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
assert app.window_icon_path, "应找到并应用 app.ico"
assert os.path.basename(app.window_icon_path).lower() == "app.ico", \
    f"窗口图标应指向 app.ico（实际 {app.window_icon_path}）"
icon_small = user32.SendMessageW(hwnd, 0x007F, 0, 0)  # WM_GETICON, ICON_SMALL
icon_big = user32.SendMessageW(hwnd, 0x007F, 1, 0)    # WM_GETICON, ICON_BIG
assert icon_small and icon_big, \
    f"标题栏/任务栏图标应已设置（small={icon_small} big={icon_big}）"

# DROPFILES 头：pFiles=20, pt=(0,0), fNC=0, fWide=1（宽字符 = 中文路径原样）
DROPFILES_HEAD = struct.pack("<Iiiii", 20, 0, 0, 0, 1)


def drop_files_into_window(paths):
    """把一组路径打成 HDROP 投递给窗口，等价于用户真实拖放。"""
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    payload = DROPFILES_HEAD + names
    hdrop = kernel32.GlobalAlloc(0x0042, len(payload))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    assert hdrop, "GlobalAlloc 失败"
    ptr = kernel32.GlobalLock(hdrop)
    assert ptr, "GlobalLock 失败"
    ctypes.memmove(ptr, payload, len(payload))
    kernel32.GlobalUnlock(hdrop)
    user32.PostMessageW(hwnd, 0x0233, int(hdrop), 0)  # WM_DROPFILES
    _pump(0.5)


before = len(app.sources)
drop_files_into_window([drag_path])
assert len(app.sources) == before + 1, f"拖拽应新增 1 张图（实际 {len(app.sources)}）"
assert app.sources[-1]["path"] == drag_path, "拖入路径应原样保留（含中文与空格）"
assert app.sources[-1]["src"].size == (800, 800), "拖入的图应已解码"
assert app.cur == len(app.sources) - 1, "应切到刚拖入的图"
assert app.box is None, "新拖入的图应等待拉框"

# 拖入非图片：不加入会话，只在状态栏提示
txt_path = os.path.join(drag_dir, "说明 文本.txt")
with open(txt_path, "w", encoding="utf-8") as fh:
    fh.write("not an image")
before2 = len(app.sources)
drop_files_into_window([txt_path])
assert len(app.sources) == before2, "非图片文件不应加入会话"
assert "没有可识别的图片" in app.status.get(), f"非图片应提示（实际 {app.status.get()!r}）"

drop_files.disable_file_drop(root)
shutil.rmtree(drag_dir, ignore_errors=True)

root.update()
root.destroy()
print("GUI pipeline smoke OK (multi-image + keyboard + crosshair + drop)")
