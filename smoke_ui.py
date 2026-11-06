# -*- coding: utf-8 -*-
"""第二部分 UI 口径回归冒烟（隐藏窗口，不需要真实棋局 / 截图 / 参照图包）。

守护的是“容易被顺手改错、又不会让程序启动报错”的那一层口径：

- 窗口默认 902x990，最小尺寸 902x860
- 右侧大标题「图像识别」
- 启动默认识别方式 = 颜色阈值；模板参数行冒号后空白
- 模板文件夹灰字：初始「未导入」；导入成功后「已导入：文件夹名」（无引号）
- 切到模板匹配但取消导入 → 自动切回颜色阈值，右下提示「导入失败」
- 导入的文件夹没有 meta.json → 切回颜色阈值，右下「导入文件夹内未找到 meta.json」
- 导入成功 → 模板参数行「模板参数：空交点X张，黑棋X张，白棋X张」（冒号后无空格）
- 切回颜色阈值 → 不保留已导入文件夹；右上「识别方式：颜色阈值」/右下「识别方式已切换为颜色阈值。」
- 手动框选 → 未点击时已有贯穿画布的绿色 #4ADE80 十字线，
  右下「点击一个角，再点击对角；范围内应包含完整网格。」

运行：python smoke_ui.py   （EXIT=0 通过）
"""

import os
import shutil
import sys
import tempfile
import tkinter as tk
import tkinter.filedialog as filedialog

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gomoku_assistant.ui as ui_module  # noqa: E402
from gomoku_assistant.ui import GomokuApp  # noqa: E402

FAILURES: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append("%s：期望 %r，实际 %r" % (name, expected, actual))


def collect_texts(widget) -> list[str]:
    texts: list[str] = []
    try:
        value = widget.cget("text")
    except tk.TclError:
        value = ""
    if value:
        texts.append(str(value))
    for child in widget.winfo_children():
        texts.extend(collect_texts(child))
    return texts


root = tk.Tk()
root.withdraw()
app = GomokuApp(root)
root.update_idletasks()

# ---- 1. 窗口尺寸（宽度下限由 minsize 兜底，所以断言实际生效的宽高，而不是 geometry 字符串）----
check("窗口最小尺寸", app.root.minsize(), (902, 860))
check("窗口默认宽度", app.root.winfo_width(), 902)
check("窗口默认高度", app.root.winfo_height(), 990)

# ---- 2. 大标题 ----
check("大标题「图像识别」出现次数", collect_texts(app.root).count("图像识别"), 1)

# ---- 3. 启动默认状态 ----
check("默认识别方式", app.recognition_mode.get(), "color")
check("初始模板文件夹灰字", app.template_folder_text.get(), "未导入")
check("初始模板参数行", app.template_status_text.get(), "模板参数：")

# ---- 4. 切到模板匹配但取消导入 → 自动切回颜色阈值 ----
filedialog.askdirectory = lambda **kwargs: ""
app.recognition_mode.set("template")
app._recognition_mode_changed()
check("取消导入后识别方式", app.recognition_mode.get(), "color")
check("取消导入右下提示", app.detail_text.get(), "导入失败")
check("取消导入后模板参数行", app.template_status_text.get(), "模板参数：")

# ---- 5. 导入的文件夹没有 meta.json → 切回颜色阈值 ----
bad_dir = tempfile.mkdtemp(prefix="gomoku-ui-smoke-")
filedialog.askdirectory = lambda **kwargs: bad_dir
app.recognition_mode.set("template")
app._recognition_mode_changed()
check("错误目录后识别方式", app.recognition_mode.get(), "color")
check("错误目录右下提示", app.detail_text.get(), "导入文件夹内未找到 meta.json")


# ---- 6. 导入成功（用假库替代真实参照图包）----
class _FakeLibrary:
    def summary(self) -> str:
        return "空交点 1 张，黑棋 2 张，白棋 3 张"


ui_module.load_template_package = lambda folder: _FakeLibrary()
filedialog.askdirectory = lambda **kwargs: os.path.join("D:\\模板库", "参照图包v1")
app.recognition_mode.set("template")
app._recognition_mode_changed()
check("成功导入后识别方式", app.recognition_mode.get(), "template")
check("成功导入后文件夹灰字", app.template_folder_text.get(), "已导入：参照图包v1")
check(
    "成功导入后模板参数行",
    app.template_status_text.get(),
    "模板参数：空交点1张，黑棋2张，白棋3张",
)
check("成功导入右上提示", app.status_text.get(), "参照图包已导入")
check(
    "成功导入右下提示",
    app.detail_text.get(),
    "空交点 1 张，黑棋 2 张，白棋 3 张；识别方式已切换为模板匹配。",
)
# 识别失败提示按模式换口径的前提：模板匹配生效时才算模板模式
check("模板模式判定", app._template_mode_active(), True)

# ---- 7. 切回颜色阈值：不保留文件夹，提示补齐 ----
app.recognition_mode.set("color")
app._recognition_mode_changed()
check("切回后不保留模板库", app.template_lib, None)
check("切回后文件夹灰字", app.template_folder_text.get(), "未导入")
check("切回后模板参数行", app.template_status_text.get(), "模板参数：")
check("切回后右上提示", app.status_text.get(), "识别方式：颜色阈值")
check("切回后右下提示", app.detail_text.get(), "识别方式已切换为颜色阈值。")
check("颜色模式判定", app._template_mode_active(), False)

# ---- 8. 手动框选：未点击就要有贯穿画布的绿色十字线 ----
app._capture_target = lambda: (np.full((800, 800, 3), 200, dtype=np.uint8), (0, 0))
app.begin_manual_selection()
check("框选状态", app._selecting, True)
check("框选右下提示", app.detail_text.get(), "点击一个角，再点击对角；范围内应包含完整网格。")
xhair = app.canvas.find_withtag("xhair")
check("未点击时十字线条数", len(xhair), 2)
colors = {app.canvas.itemcget(item, "fill").lower() for item in xhair}
check("十字线颜色", colors, {"#4ade80"})

app.close()
shutil.rmtree(bad_dir, ignore_errors=True)

if FAILURES:
    print("UI 口径冒烟失败（%d 项）：" % len(FAILURES))
    for item in FAILURES:
        print("  - " + item)
    raise SystemExit(1)

print("UI 口径冒烟 OK（窗口/文案/默认状态/模式切换/框选十字线）")
