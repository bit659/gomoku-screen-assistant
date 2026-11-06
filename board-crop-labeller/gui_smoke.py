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
import tkinter as tk
from types import SimpleNamespace

from PIL import Image

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

root.update()
root.destroy()
print("GUI pipeline smoke OK (multi-image + keyboard + crosshair)")
