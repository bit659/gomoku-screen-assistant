# -*- coding: utf-8 -*-
"""五子棋交点小图标注工具 —— 软件第一部分

功能流程：
  1. 导入一张或多张真实五子棋小程序截图（PNG/JPG/BMP/WEBP）。
     多图用于收集不同形态的样本（如"带最新落子标志"的黑/白棋
     与普通棋子不在同一张截图上出现）。
  2. 对每张图分别交互式拉框（顶部下拉框切换当前图）：
       - 左键点击 / 回车 = 选框角：第一击定第一个角，移动鼠标
         （或方向键微调）正方形跟随显示，第二击完成；
       - 右键拖动 = 平移画面；
       - 滚轮 = 以光标为中心缩放；
       - 方向键 = 按图像坐标微调逻辑光标（Shift=5px），回车=点击；
       - 贯穿整个画布的橙色十字线 = 当前光标位置。
  3. 拉框完成自动分割：按"半格平移 + 外圈外扩半个网格"裁剪出
     15x15 = 225 个交点小图，并在原图上叠加显示分割线。
  4. 人工分类：空交点 / 黑棋 / 白棋，每个类目可多选小图
     （点击小图 = 加入/移出当前类目，一个交点最多归属一个类目）；
     每张图的分类状态相互独立。
  5. 保存参照图包：把**所有图片**选中的样本合并导出到
     empty / black / white 三个子目录 + meta.json（文件名带图序号防冲突），
     供第二部分自动识别软件加载比对。

运行：python main.py
依赖：Pillow（pip install pillow），tkinter（Python 自带）。
"""
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import crop_math
import drop_files

# 允许导入的图片扩展名（导入对话框与拖拽共用同一套判断）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".jfif")

GRID_N = crop_math.GRID_N
THUMB = 44  # 小图缩略图边长（像素）
CELL_PX = THUMB  # 小图格与小图同尺寸：图片铺满格子，外框与图片之间没有白边
CELL_PAD = 3  # 格子之间的固定间距（初始间距即恒定，选择不会改变布局）

# 三个分类：(内部键, 显示名, 高亮颜色)
CATS = [
    ("empty", "空交点", "#f5a623"),
    ("black", "黑棋", "#2f6fd6"),
    ("white", "白棋", "#e14d4d"),
]
CAT_COLOR = {k: c for k, _, c in CATS}
CAT_NAME = {k: n for k, n, _ in CATS}
CAT_KEYS = [k for k, _, _ in CATS]


def _empty_selected():
    return {k: set() for k in CAT_KEYS}


class App:
    """主程序：多图会话 + 原图画布 + 225 小图标注页。"""

    def __init__(self, root):
        self.root = root
        root.title("棋盘分割标注工具")
        root.geometry("1280x840")
        self.base_dir = os.path.dirname(os.path.abspath(__file__))

        # ---- 多图会话状态 ----
        self.sources = []   # 每张截图一个 dict（见 load_images / show_source）
        self.cur = 0        # 当前图下标

        # ---- 当前图视图（引用 self.sources[self.cur] 的数据） ----
        self.src = None
        self.path = None
        self.box = None
        self.crops = None
        self.rects = None
        self.stats = None
        self.selected = _empty_selected()   # 当前图的分类集合（引用）

        # ---- 画布交互状态 ----
        self.scale = 1.0
        self.min_scale = 0.05
        self.ox = 0.0
        self.oy = 0.0
        self.drawing = False     # 是否正在拉框（已点第一个角，等待第二击）
        self.anchor = None       # 拉框第一个角的图像坐标
        self.last_cv = None      # 最近一次鼠标的画布坐标
        self.cursor_cv = None    # 逻辑光标画布坐标（鼠标/方向键共同控制）
        self.rubber = None       # 跟随显示的正方形矩形 item
        self.xh = None           # 贯穿十字线 (竖线item, 横线item)
        self.pan_xy = None       # 右键平移起点（画布坐标）
        self.photo = None        # 主画布 PhotoImage 引用（防 GC）
        self._fast_after_id = None    # 交互重绘节流计时器（快速 BILINEAR）
        self._refine_after_id = None  # 停止交互后的 LANCZOS 精化计时器
        self.cur_cat = "empty"   # 当前分类
        self.thumbs = []         # 225 个格子 (canvas, img_item, border_item)
        self.thumb_photos = []   # 缩略图 PhotoImage 引用（防 GC）

        self._build_ui()
        # 允许把图片直接拖进窗口导入（Win32 拖放，打包后同样有效）
        self.drop_enabled = drop_files.enable_file_drop(self.root, self.on_drop_paths)
        # 窗口左上角/任务栏图标：换掉 Tk 默认的羽毛图标
        self.window_icon_path = self._set_window_icon()
        self.status.set("请先导入一张或多张五子棋截图（也可以直接把图片拖进窗口）。")

    # ------------------------------------------------------------------
    # 窗口图标
    # ------------------------------------------------------------------
    @staticmethod
    def _icon_candidates():
        """app.ico 的候选位置：打包内嵌目录 -> 源码目录 -> exe 同目录。"""
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "app.ico"))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
        if getattr(sys, "frozen", False):
            candidates.append(os.path.join(os.path.dirname(sys.executable), "app.ico"))
        return candidates

    def _set_window_icon(self):
        """把 Tk 默认的羽毛图标换成交点图标；返回实际生效的 ico 路径（找不到返回 None）。"""
        for path in self._icon_candidates():
            if not os.path.isfile(path):
                continue
            try:
                self.root.iconbitmap(path)
                self.root.iconbitmap(default=path)  # 之后弹出的对话框/顶层窗口也用同一个
            except tk.TclError:
                continue
            return path
        return None

    # ------------------------------------------------------------------
    # UI 搭建
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ===== 顶部：多图管理（两个页面共用） =====
        top = ttk.Frame(self.root, padding=(6, 4, 6, 2))
        top.pack(fill="x")
        ttk.Button(top, text="导入图片（可多选追加）",
                   command=self.load_images).pack(side="left")
        ttk.Label(top, text="当前图片：").pack(side="left", padx=(10, 0))
        self.img_combo = ttk.Combobox(top, state="readonly", width=40)
        self.img_combo.pack(side="left")
        self.img_combo.bind("<<ComboboxSelected>>", self.on_combo)
        self.source_label = tk.StringVar()
        ttk.Label(top, textvariable=self.source_label,
                  foreground="#555").pack(side="left", padx=(12, 0))

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.notebook = nb
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ===== 页1：原图与拉框 =====
        tab_board = ttk.Frame(nb)
        self.tab_board = tab_board
        nb.add(tab_board, text="① 原图与拉框")

        bar = ttk.Frame(tab_board, padding=(6, 6, 6, 2))
        bar.pack(fill="x")
        ttk.Button(bar, text="清除当前分割线并重新拉框",
                   command=self.clear_and_reselect).pack(side="left")
        ttk.Button(bar, text="导出分割线预览图",
                   command=self.export_preview).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="滚轮=缩放  右键按住拖动=平移  左键/回车=选定框角  方向键可用  拖入图片=导入",
                  foreground="#555").pack(side="right")

        self.canvas = tk.Canvas(tab_board, bg="#202020", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Button-3>", self.on_rpress)
        self.canvas.bind("<B3-Motion>", self.on_rmove)
        self.canvas.bind("<ButtonRelease-3>", self.on_rrelease)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Configure>", lambda e: self._schedule_redraw(False))
        for key in ("<Left>", "<Right>", "<Up>", "<Down>"):
            self.canvas.bind(key, self.on_key)
        for key in ("<Return>", "<KP_Enter>"):
            self.canvas.bind(key, self.on_return)

        # ===== 页2：225 交点标注 =====
        tab_crops = ttk.Frame(nb)
        nb.add(tab_crops, text="② 225 交点小图标注")

        cbar = ttk.Frame(tab_crops, padding=(6, 6, 6, 2))
        cbar.pack(fill="x")

        ttk.Label(cbar, text="当前分类：").pack(side="left")
        self.cat_buttons = {}
        for key, name, color in CATS:
            b = tk.Button(cbar, text=name, width=8,
                          command=lambda k=key: self.set_cat(k))
            b.pack(side="left", padx=(0, 4))
            self.cat_buttons[key] = b
        ttk.Button(cbar, text="重新生成 225 小图",
                   command=self.generate).pack(side="left", padx=(10, 0))
        ttk.Button(cbar, text="清空当前类目",
                   command=self.clear_current).pack(side="left", padx=(6, 0))
        ttk.Button(cbar, text="清空当前图全部",
                   command=self.clear_all).pack(side="left", padx=(6, 0))
        ttk.Button(cbar, text="保存参照图包（合并所有图）",
                   command=self.save_package).pack(side="left", padx=(16, 0))
        ttk.Label(cbar, text="点击小图 = 加入/移出当前类目（每张图独立）",
                  foreground="#555").pack(side="right")

        # 缩略图滚动区域
        self.thumb_canvas = tk.Canvas(tab_crops, bg="#f2f2f2", highlightthickness=0)
        vsb = ttk.Scrollbar(tab_crops, orient="vertical",
                            command=self.thumb_canvas.yview)
        self.thumb_canvas.configure(yscrollcommand=vsb.set)
        self.thumb_canvas.pack(side="left", fill="both", expand=True,
                               padx=(6, 0), pady=(4, 6))
        vsb.pack(side="left", fill="y", pady=(4, 6))
        self.thumb_canvas.bind("<MouseWheel>",
                               lambda e: self._thumb_scroll(e))

        self.thumb_grid = tk.Frame(self.thumb_canvas, bg="#f2f2f2")
        self._thumb_window = self.thumb_canvas.create_window(
            (0, 0), window=self.thumb_grid, anchor="nw")
        self.thumb_grid.bind("<MouseWheel>", lambda e: self._thumb_scroll(e))
        self.thumb_canvas.bind("<Configure>",
                               lambda e: self.thumb_canvas.itemconfigure(
                                   self._thumb_window, width=e.width))

        # ===== 底部状态栏 =====
        self.status = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status, anchor="w",
                  relief="sunken", padding=(6, 2)).pack(side="bottom", fill="x")

        self.set_cat("empty")
        self.update_combo()
        self.update_cat_counts()

    # ------------------------------------------------------------------
    # 多图会话：导入 / 切换 / 状态
    # ------------------------------------------------------------------
    def load_images(self):
        paths = filedialog.askopenfilenames(
            title="选择一张或多张五子棋截图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp"),
                       ("所有文件", "*.*")],
            initialdir=self.base_dir)
        if not paths:
            return
        self.add_paths(paths)

    def on_drop_paths(self, paths):
        """拖拽回调：把拖进窗口的图片按导入流程加入会话（中文路径原样可用）。"""
        self.add_paths(paths, source="拖入")

    def add_paths(self, paths, source="导入"):
        """把给定的图片路径加入多图会话；打不开的文件最后汇总提示一次。"""
        images = [p for p in paths if p.lower().endswith(IMAGE_EXTENSIONS)]
        if not images:
            if source == "拖入":
                self.status.set("拖入的内容里没有可识别的图片（支持 PNG/JPG/BMP/WEBP）。")
            else:
                self.status.set("未选择有效的图片文件。")
            return
        added = 0
        failed = []
        for path in images:
            try:
                src = Image.open(path)
                src.load()
                src = src.convert("RGB")
            except Exception as exc:
                failed.append(f"{os.path.basename(path)}：{exc}")
                continue
            self.sources.append({
                "path": path,
                "src": src,
                "box": None,
                "crops": None,
                "rects": None,
                "stats": None,
                "selected": _empty_selected(),
            })
            added += 1
        if added:
            self.cur = len(self.sources) - 1
            self.show_source(self.cur)
            self.status.set(f"{source} {added} 张，当前共 {len(self.sources)} 张。")
        if failed:
            head = "\n".join(failed[:8])
            tail = "" if len(failed) <= 8 else f"\n…另有 {len(failed) - 8} 个文件打不开"
            messagebox.showerror("无法打开图片", head + tail)

    def show_source(self, idx):
        """切换到第 idx 张图，把它的数据同步为当前视图。"""
        s = self.sources[idx]
        self.cur = idx
        self.src = s["src"]
        self.path = s["path"]
        self.box = s["box"]
        self.crops = s["crops"]
        self.rects = s["rects"]
        self.stats = s["stats"]
        self.selected = s["selected"]
        self.drawing = False
        self.anchor = None
        self.last_cv = None
        self.cursor_cv = None
        self.xh = None
        self._fit()
        self._schedule_redraw(False)
        self.canvas.focus_set()
        self.update_combo()
        self.update_source_label()
        if self.crops is not None:
            self.build_thumbs()
            self.update_cat_counts()
        else:
            self.clear_thumbs()
            self.status.set(f"图片 {idx + 1}/{len(self.sources)}："
                            f"{os.path.basename(s['path'])} 尚未拉框，请到页①拉框。")

    def on_combo(self, e):
        i = self.img_combo.current()
        if 0 <= i < len(self.sources):
            self.show_source(i)

    def _on_tab_changed(self, e):
        """切回页①时把键盘焦点还给画布，保证方向键/回车可用。"""
        if self.notebook.select() == str(self.tab_board) and self.src is not None:
            self.canvas.focus_set()

    def update_combo(self):
        vals = [f"{i + 1}. {os.path.basename(s['path'])}"
                for i, s in enumerate(self.sources)]
        self.img_combo.configure(values=vals)
        if vals:
            self.img_combo.current(self.cur)
        else:
            self.img_combo.set("")

    def update_source_label(self):
        if not self.sources:
            self.source_label.set("")
            return
        s = self.sources[self.cur]
        if s["box"] is None:
            state = "未拉框"
        else:
            state = f"已生成 {len(s['crops'])} 小图"
        self.source_label.set(f"图 {self.cur + 1}/{len(self.sources)} · {state}")

    # ------------------------------------------------------------------
    # 坐标变换
    # ------------------------------------------------------------------
    def img2cv(self, p):
        """图像坐标 -> 画布坐标。"""
        return (self.ox + p[0] * self.scale, self.oy + p[1] * self.scale)

    def cv2img(self, p):
        """画布坐标 -> 图像坐标。"""
        return ((p[0] - self.ox) / self.scale, (p[1] - self.oy) / self.scale)

    # ------------------------------------------------------------------
    # 图片适配 / 重绘 / 贯穿十字线
    # ------------------------------------------------------------------
    def _fit(self):
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        iw, ih = self.src.width, self.src.height
        self.scale = min(cw / iw, ch / ih) * 0.94
        self.min_scale = self.scale * 0.2
        self.ox = (cw - iw * self.scale) / 2
        self.oy = (ch - ih * self.scale) / 2

    def redraw(self, fast=False):
        """按当前 scale 重采样原图并重绘全画面。
        fast=True 用 BILINEAR（交互缩放/平移时用，快 5~10 倍）；
        fast=False 用 LANCZOS（静态/最终精化，画质最好）。"""
        c = self.canvas
        c.delete("all")
        self.xh = None
        self.rubber = None
        if self.src is None:
            return
        w = max(1, round(self.src.width * self.scale))
        h = max(1, round(self.src.height * self.scale))
        resample = (Image.Resampling.BILINEAR if fast
                    else Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(self.src.resize((w, h), resample))
        c.create_image(self.ox, self.oy, anchor="nw", image=self.photo)
        self.draw_overlay()
        # 正在拉框时：重建跟随正方形（缩放/平移后自动跟随逻辑光标）
        if self.drawing and self.anchor is not None and self.cursor_cv is not None:
            box = crop_math.make_square_box(
                self.anchor, self.cv2img(self.cursor_cv))
            if box:
                p0 = self.img2cv((box[0], box[1]))
                p1 = self.img2cv((box[2], box[3]))
                self.rubber = c.create_rectangle(
                    *p0, *p1, outline="#0B6B37", width=2, tags="rubber")
        self.refresh_xhair()

    def _schedule_redraw(self, fast):
        """交互重绘调度：缩放/平移走“节流 + 双采样”，静态走即时 LANCZOS。
        - fast=True（滚轮/右键拖动）：30ms 窗口内合并多次事件为一次
          BILINEAR 快速重绘；停止交互 230ms 后再 LANCZOS 精化一次。
        - fast=False（加载/切图/窗口缩放）：取消挂起的交互任务，立即精绘。"""
        if self._refine_after_id is not None:
            self.root.after_cancel(self._refine_after_id)
            self._refine_after_id = None
        if not fast:
            if self._fast_after_id is not None:
                self.root.after_cancel(self._fast_after_id)
                self._fast_after_id = None
            self.redraw(fast=False)
            return
        if self._fast_after_id is None:
            self._fast_after_id = self.root.after(30, self._do_fast_redraw)
        self._refine_after_id = self.root.after(230, self._do_refine_redraw)

    def _do_fast_redraw(self):
        self._fast_after_id = None
        self.redraw(fast=True)

    def _do_refine_redraw(self):
        self._refine_after_id = None
        if self._fast_after_id is not None:
            return  # 新交互又来了，精化留给下一轮
        self.redraw(fast=False)

    def refresh_xhair(self):
        """绘制/移动贯穿整个画布的十字参考线（橙色虚线）。"""
        c = self.canvas
        cv = self.cursor_cv
        if self.src is None or cv is None:
            c.delete("xhair")
            self.xh = None
            return
        w = c.winfo_width()
        h = c.winfo_height()
        x, y = cv
        if self.xh is not None:
            v_id, h_id = self.xh
            c.coords(v_id, x, 0, x, h)
            c.coords(h_id, 0, y, w, y)
        else:
            v_id = c.create_line(x, 0, x, h, fill="#ff6a00", width=1,
                                 dash=(4, 3), tags="xhair")
            h_id = c.create_line(0, y, w, y, fill="#ff6a00", width=1,
                                 dash=(4, 3), tags="xhair")
            self.xh = (v_id, h_id)

    # ------------------------------------------------------------------
    # 分割线覆盖层
    # ------------------------------------------------------------------
    def draw_overlay(self):
        c = self.canvas
        c.delete("overlay")
        if self.src is None or self.box is None:
            return
        bounds_x, bounds_y, cell, _ = crop_math.compute_crop_grid(self.box)
        lw_thin = max(1, round(self.scale * 0.6))
        lw_mid = max(1, round(self.scale * 1.2))
        lw_box = max(2, round(self.scale * 1.8))

        x0, y0, x1, y1 = self.box
        for k in range(GRID_N):
            x = x0 + k * cell
            c.create_line(*self.img2cv((x, y0)), *self.img2cv((x, y1)),
                          fill="#ff9d2e", width=lw_thin, tags="overlay")
            y = y0 + k * cell
            c.create_line(*self.img2cv((x0, y)), *self.img2cv((x1, y)),
                          fill="#ff9d2e", width=lw_thin, tags="overlay")
        for x in bounds_x:
            c.create_line(*self.img2cv((x, bounds_y[0])),
                          *self.img2cv((x, bounds_y[GRID_N])),
                          fill="#00d0ff", width=lw_mid, tags="overlay")
        for y in bounds_y:
            c.create_line(*self.img2cv((bounds_x[0], y)),
                          *self.img2cv((bounds_x[GRID_N], y)),
                          fill="#00d0ff", width=lw_mid, tags="overlay")
        c.create_rectangle(*self.img2cv((x0, y0)), *self.img2cv((x1, y1)),
                           outline="#0B6B37", width=lw_box, tags="overlay")

    # ------------------------------------------------------------------
    # 鼠标交互：点击式拉框、右键平移、滚轮缩放
    # ------------------------------------------------------------------
    def click_at(self, cv):
        """逻辑点击（鼠标左键或回车共用）：第一击开始拉框，第二击完成。"""
        if self.src is None:
            return
        self.cursor_cv = cv
        self.last_cv = cv
        self.refresh_xhair()
        if not self.drawing:
            # 第一击：开始新拉框，清除上一次的黄框/橙线/青线
            self.drawing = True
            self.box = None
            self.canvas.delete("overlay")
            self.anchor = self.cv2img(cv)
            self.rubber = self.canvas.create_rectangle(
                *cv, *cv, outline="#0B6B37", width=2, tags="rubber")
            self.status.set("已设定第一个框角：移动鼠标或方向键微调，"
                            "正方形跟随，回车或左键第二击完成")
        else:
            # 第二击：完成
            box = crop_math.make_square_box(self.anchor, self.cv2img(cv))
            self.drawing = False
            self.anchor = None
            if self.rubber is not None:
                self.canvas.delete(self.rubber)
                self.rubber = None
            if box is None:
                return
            self.box = box
            self.draw_overlay()
            self.generate()  # 拉框完成即自动分割 225 个小图并跳转标注页

    def on_click(self, e):
        self.canvas.focus_set()
        self.click_at((e.x, e.y))

    def on_motion(self, e):
        if self.src is None:
            return
        self.cursor_cv = (e.x, e.y)
        self.last_cv = (e.x, e.y)
        self.refresh_xhair()
        if self.drawing and self.anchor is not None and self.rubber is not None:
            box = crop_math.make_square_box(
                self.anchor, self.cv2img((e.x, e.y)))
            if box:
                p0 = self.img2cv((box[0], box[1]))
                p1 = self.img2cv((box[2], box[3]))
                self.canvas.coords(self.rubber, *p0, *p1)

    def on_rpress(self, e):
        if self.src is None:
            return
        self.pan_xy = (e.x, e.y)
        self.last_cv = (e.x, e.y)
        self.canvas.config(cursor="fleur")

    def on_rmove(self, e):
        if self.pan_xy is None:
            return
        dx, dy = e.x - self.pan_xy[0], e.y - self.pan_xy[1]
        self.pan_xy = (e.x, e.y)
        self.last_cv = (e.x, e.y)
        self.ox += dx
        self.oy += dy
        self._keep_cursor_attached()
        self._schedule_redraw(True)

    def on_rrelease(self, e):
        self.pan_xy = None
        self.canvas.config(cursor="")

    def on_wheel(self, e):
        if self.src is None:
            return
        self.last_cv = (e.x, e.y)
        f = 1.25 if e.delta > 0 else 1 / 1.25
        ns = max(self.min_scale, min(40.0, self.scale * f))
        if ns == self.scale:
            return
        # 保持光标下的图像点不动
        ix = (e.x - self.ox) / self.scale
        iy = (e.y - self.oy) / self.scale
        self.scale = ns
        self.ox = e.x - ix * ns
        self.oy = e.y - iy * ns
        self._keep_cursor_attached()
        self._schedule_redraw(True)

    def _keep_cursor_attached(self):
        """平移/缩放后，让逻辑光标仍钉在同一个图像点上。"""
        if self.cursor_cv is not None:
            img_pt = self.cv2img(self.cursor_cv)
            self.cursor_cv = self.img2cv(img_pt)

    # ------------------------------------------------------------------
    # 键盘：方向键微调逻辑光标（图像坐标），回车=点击
    # ------------------------------------------------------------------
    def on_key(self, e):
        if self.src is None:
            return "break"
        step_img = 5 if (e.state & 0x0001) else 0.5  # Shift → 5 图像像素；默认 0.5（亚像素）
        step = {
            "Left": (-step_img, 0),
            "Right": (step_img, 0),
            "Up": (0, -step_img),
            "Down": (0, step_img),
        }.get(e.keysym)
        if step is None:
            return "break"
        if self.cursor_cv is None:
            # 尚未有过光标：先放到图像中心
            self.cursor_cv = self.img2cv(
                (self.src.width / 2, self.src.height / 2))
        cx, cy = self.cursor_cv
        cx += step[0] * self.scale
        cy += step[1] * self.scale
        # 钳制在图像显示范围内
        x0, y0 = self.ox, self.oy
        x1 = self.ox + self.src.width * self.scale
        y1 = self.oy + self.src.height * self.scale
        cx = min(max(cx, x0), x1)
        cy = min(max(cy, y0), y1)
        self.cursor_cv = (cx, cy)
        self.last_cv = (cx, cy)
        self.refresh_xhair()
        if self.drawing and self.anchor is not None:
            box = crop_math.make_square_box(
                self.anchor, self.cv2img((cx, cy)))
            if box and self.rubber is not None:
                p0 = self.img2cv((box[0], box[1]))
                p1 = self.img2cv((box[2], box[3]))
                self.canvas.coords(self.rubber, *p0, *p1)
        ix, iy = self.cv2img((cx, cy))
        self.status.set(f"光标图像坐标 ({ix:.2f}, {iy:.2f})，"
                        f"方向键微调（Shift=5px），回车=点击")
        return "break"

    def on_return(self, e):
        if self.src is None:
            return "break"
        if self.cursor_cv is None:
            self.cursor_cv = self.img2cv(
                (self.src.width / 2, self.src.height / 2))
            self.refresh_xhair()
        self.click_at(self.cursor_cv)
        return "break"

    # ------------------------------------------------------------------
    # 生成 225 个小图（写入当前图数据）
    # ------------------------------------------------------------------
    def generate(self):
        if self.src is None or self.box is None:
            return
        crops, rects, stats = crop_math.crop_intersections(self.src, self.box)
        self.crops, self.rects, self.stats = crops, rects, stats
        self.selected = _empty_selected()
        s = self.sources[self.cur]
        s["box"] = self.box
        s["crops"] = crops
        s["rects"] = rects
        s["stats"] = stats
        s["selected"] = self.selected
        self.build_thumbs()
        self.update_cat_counts()
        self.notebook.select(1)
        self.update_source_label()
        msg = f"图 {self.cur + 1}：已生成 {len(crops)} 个交点小图"
        msg += f"（每小图约 {stats['cell']:.1f}x{stats['cell']:.1f} 像素）"
        if stats["clamped"]:
            msg += (f"。注意：{stats['clamped']} 个因贴近图像边缘被钳制，"
                    "建议拉框四周离图片边缘留出至少半个网格的余量。")
        self.status.set(msg)

    def clear_and_reselect(self):
        """清除当前图的分割线（网格/裁剪线/拉框）与 225 小图，回到页①等待重新拉框。"""
        if self.src is None:
            self.status.set("请先导入图片。")
            return
        self.drawing = False
        self.anchor = None
        self.box = None
        if self.rubber is not None:
            self.canvas.delete(self.rubber)
            self.rubber = None
        self.canvas.delete("overlay")
        self.canvas.delete("rubber")
        self.crops = None
        self.rects = None
        self.stats = None
        self.selected = _empty_selected()
        s = self.sources[self.cur]
        s["box"] = None
        s["crops"] = None
        s["rects"] = None
        s["stats"] = None
        s["selected"] = self.selected
        self.clear_thumbs()
        self.notebook.select(0)
        self.canvas.focus_set()
        self._schedule_redraw(False)
        self.update_source_label()
        self.status.set("已清除当前分割线，请重新点击两个框角（左键/回车）拉框")

    # ------------------------------------------------------------------
    # 缩略图网格
    # ------------------------------------------------------------------
    def build_thumbs(self):
        for w in self.thumb_grid.winfo_children():
            w.destroy()
        self.thumbs = []
        self.thumb_photos = []
        for r in range(GRID_N):
            row = []
            for c in range(GRID_N):
                small = self.crops[r * GRID_N + c].resize(
                    (THUMB, THUMB), Image.Resampling.LANCZOS)
                ph = ImageTk.PhotoImage(small)
                self.thumb_photos.append(ph)
                cell = tk.Canvas(self.thumb_grid, width=CELL_PX,
                                 height=CELL_PX, bg="#f2f2f2",
                                 highlightthickness=0, cursor="hand2")
                cell.grid(row=r, column=c, padx=CELL_PAD, pady=CELL_PAD)
                img_item = cell.create_image(
                    CELL_PX // 2, CELL_PX // 2, image=ph)
                border_item = cell.create_rectangle(
                    1, 1, CELL_PX - 1, CELL_PX - 1,
                    outline="#c0c0c0", width=2)
                cell.bind("<Button-1>",
                          lambda ev, rr=r, cc=c: self.toggle_crop(rr, cc))
                cell.bind("<Enter>",
                          lambda ev, rr=r, cc=c: self.hover_thumb(rr, cc))
                cell.bind("<Leave>", lambda ev: self.hover_leave())
                cell.bind("<MouseWheel>", lambda ev: self._thumb_scroll(ev))
                row.append((cell, img_item, border_item))
                # 按当前图的分类状态初始化边框（切回已标注的图时恢复显示）
                self._paint_cell(cell, border_item, r, c)
            self.thumbs.append(row)
        self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all"))

    def clear_thumbs(self):
        for w in self.thumb_grid.winfo_children():
            w.destroy()
        self.thumbs = []
        self.thumb_photos = []
        self.thumb_canvas.configure(scrollregion=(0, 0, 0, 0))

    def _thumb_scroll(self, e):
        self.thumb_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _set_cell_border(self, cell, border_item, color, width):
        """边框线完全落在格子内侧：路径内缩半个线宽，四条边等宽且对称。"""
        half = width / 2
        cell.coords(border_item, half, half, CELL_PX - half, CELL_PX - half)
        cell.itemconfigure(border_item, outline=color, width=width)

    def _paint_cell(self, cell, border_item, r, c):
        """按当前图的分类状态设置单个格子边框颜色（尺寸不变）。"""
        for key, _, color in CATS:
            if (r, c) in self.selected[key]:
                self._set_cell_border(cell, border_item, color, 3)
                return
        self._set_cell_border(cell, border_item, "#c0c0c0", 2)

    def paint_thumb(self, r, c):
        cell, _, border_item = self.thumbs[r][c]
        self._paint_cell(cell, border_item, r, c)

    def toggle_crop(self, r, c):
        key = (r, c)
        cur = self.selected[self.cur_cat]
        if key in cur:
            cur.discard(key)
        else:
            for k in CAT_KEYS:
                self.selected[k].discard(key)   # 一个交点只归一类
            cur.add(key)
        self.paint_thumb(r, c)
        self.update_cat_counts()

    def hover_thumb(self, r, c):
        name = os.path.basename(self.path or "")
        self.status.set(f"图 {self.cur + 1}「{name}」 第 {r + 1} 行 / 第 {c + 1} 列 交点  "
                        f"（裁剪区域 {self.rects[r * GRID_N + c]}）  "
                        f"点击加入「{CAT_NAME[self.cur_cat]}」")

    def hover_leave(self):
        self.update_cat_counts()

    # ------------------------------------------------------------------
    # 分类操作（作用于当前图）
    # ------------------------------------------------------------------
    def set_cat(self, key):
        self.cur_cat = key
        for k, _, color in CATS:
            b = self.cat_buttons[k]
            if k == key:
                b.config(bg=color, fg="white", relief="sunken")
            else:
                b.config(bg="#f0f0f0", fg="black", relief="raised")

    def clear_current(self):
        self.selected[self.cur_cat].clear()
        self._repaint_all_thumbs()
        self.update_cat_counts()

    def clear_all(self):
        for k in CAT_KEYS:
            self.selected[k].clear()
        self._repaint_all_thumbs()
        self.update_cat_counts()

    def _repaint_all_thumbs(self):
        for r in range(GRID_N):
            for c in range(GRID_N):
                self.paint_thumb(r, c)

    def update_cat_counts(self):
        if not self.sources:
            self.status.set("请先导入图片。")
            return
        parts = [f"{CAT_NAME[k]} {len(self.selected[k])}" for k in CAT_KEYS]
        self.status.set(f"图 {self.cur + 1}/{len(self.sources)}："
                        + "   |   ".join(parts)
                        + "   ——  点击小图加入/移出当前类目，"
                          "多张图的样本会自动合并进同一个参照图包")

    # ------------------------------------------------------------------
    # 保存参照图包（合并所有图）/ 导出预览
    # ------------------------------------------------------------------
    def save_package(self):
        if not self.sources:
            messagebox.showwarning("尚未导入", "请先导入至少一张截图。")
            return
        # 统计所有已拉框图的总样本数
        included = [s for s in self.sources
                    if s["box"] is not None and s["crops"] is not None]
        if not included:
            messagebox.showwarning("尚未生成",
                                   "请先在页①对图片拉框生成 225 个小图。")
            return
        total = sum(len(s["selected"][k]) for s in included for k in CAT_KEYS)
        if total == 0:
            messagebox.showwarning("未选择",
                                   "请先在三个类目中点击选择小图，再保存。")
            return
        default = os.path.join(
            self.base_dir, "参照图包_" + time.strftime("%Y%m%d_%H%M%S"))
        out = filedialog.askdirectory(
            title="选择参照图包保存目录（将合并所有图生成 empty/black/white + meta.json）",
            initialdir=default)
        if not out:
            return
        sources_meta = [{
            "name": os.path.basename(s["path"]),
            "img_size": s["src"].size,
            "box": s["box"],
            "stats": s["stats"],
            "crops": s["crops"],
            "selected": {k: list(s["selected"][k]) for k in CAT_KEYS},
        } for s in included]
        cat_display = {k: CAT_NAME[k] for k in CAT_KEYS}
        crop_math.save_reference_package_multi(out, sources_meta, cat_display)
        self.status.set(f"参照图包已保存：{out}"
                        f"（{len(included)} 张图，共 {total} 张样本）")
        messagebox.showinfo(
            "保存完成",
            f"参照图包已保存到：\n{out}\n\n"
            f"合并 {len(included)} 张截图，共 {total} 张参照小图，"
            f"meta.json 已生成，可直接供第二部分识别软件加载使用。")

    def export_preview(self):
        if self.src is None or self.box is None:
            messagebox.showwarning("尚未拉框", "请先在图上拉一个正方形框。")
            return
        bounds_x, bounds_y, _, _ = crop_math.compute_crop_grid(self.box)
        preview = crop_math.draw_overlay(self.src, self.box,
                                         bounds_x, bounds_y)
        default = os.path.join(
            self.base_dir,
            os.path.splitext(os.path.basename(self.path or "preview"))[0]
            + "_分割线预览.png")
        path = filedialog.asksaveasfilename(
            title="导出分割线预览图", defaultextension=".png",
            initialfile=os.path.basename(default),
            filetypes=[("PNG 图片", "*.png")])
        if not path:
            return
        preview.save(path)
        self.status.set(f"分割线预览图已导出：{path}")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
