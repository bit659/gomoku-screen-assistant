# -*- coding: utf-8 -*-
"""交点小图标注工具 —— 核心几何与图包逻辑（无 GUI 依赖，可单测）。

关键换算（15x15 棋盘 = 225 个交点，14 个网格间隔）：
- 用户在图上拉出的正方形框 box 覆盖棋盘外沿（最外圈交点的中心连线）。
- 若直接在框内等分 14 段，只能截到"格点方块"；而每个交点小图要以交点
  为中心、向四周各外扩半个网格边长。因此裁剪边界 = 15x15 网格线整体
  平移半格，且外圈再向外延伸半格：
      bounds[k] = box边缘 + (k - 0.5) * cell，k = 0..15
  共 16 条边界 × 16 条边界 = 15x15 = 225 个小图。
"""
import json
import os

from PIL import Image, ImageDraw

GRID_N = 15  # 每边交点数量


# --------------------------------------------------------------------------
# 网格计算
# --------------------------------------------------------------------------

def compute_crop_grid(box, n=GRID_N):
    """把拉框正方形换算成 225 个交点小图的裁剪网格。

    参数:
        box: (x0, y0, x1, y1)，图像像素坐标下的正方形拉框。
        n:   每边交点数量，默认 15。
    返回:
        (bounds_x, bounds_y, cell, half)
        bounds_x / bounds_y: 各 n+1 条裁剪边界（浮点坐标），
                             相对 15x15 网格线外扩了半个网格边长。
        cell: 一个网格边长（像素）。
        half: 半个网格边长（像素）。
    """
    x0, y0, x1, y1 = box
    side = x1 - x0          # 拉框边长（应等于 y1-y0，正方形）
    cell = side / (n - 1)   # 一个网格边长：14 段均分
    half = cell / 2.0       # 半个网格边长（外扩量）
    bounds_x = [x0 - half + i * cell for i in range(n + 1)]
    bounds_y = [y0 - half + i * cell for i in range(n + 1)]
    return bounds_x, bounds_y, cell, half


def make_square_box(anchor, current):
    """拉框时强制正方形：以主轴（|dx|、|dy| 较大者）决定边长。

    参数:
        anchor:  按下鼠标时的起点 (ax, ay)。
        current: 当前鼠标位置 (cx, cy)。
    返回:
        规范化正方形 (x0, y0, x1, y1)，保证 x1>x0、y1>y0；
        原地不动时返回 None。
    """
    ax, ay = anchor
    cx, cy = current
    dx, dy = cx - ax, cy - ay
    side = max(abs(dx), abs(dy))
    if side == 0:
        return None
    # 主轴方向决定整框方向；另一轴跟随主轴保持正方形
    sx = 1 if dx >= 0 else -1
    sy = 1 if dy >= 0 else -1
    if dx == 0:
        sx = sy
    if dy == 0:
        sy = sx
    x0, x1 = (ax, ax + side * sx)
    y0, y1 = (ay, ay + side * sy)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


# --------------------------------------------------------------------------
# 裁剪
# --------------------------------------------------------------------------

def crop_intersections(img, box, n=GRID_N):
    """按网格从原图裁剪 225 个交点小图。

    参数:
        img:  PIL 图像（原图）。
        box:  正方形拉框 (x0, y0, x1, y1)。
        n:    每边交点数量，默认 15。
    返回:
        (crops, rects, stats)
        crops: 长度 n*n 的 PIL 小图列表，行优先（第 r*n+c 个 = 第 r 行第 c 列）。
        rects: 对应 (l, t, r, b) 图像坐标，已钳制到图像范围内。
        stats: {"cell", "half", "clamped"}，clamped 为越界被钳制的数量。
    """
    bounds_x, bounds_y, cell, half = compute_crop_grid(box, n)
    w, h = img.size
    crops, rects = [], []
    clamped = 0
    for r in range(n):
        for c in range(n):
            l = round(bounds_x[c])
            t = round(bounds_y[r])
            rb = round(bounds_x[c + 1])
            bb = round(bounds_y[r + 1])
            cl, ct = max(0, l), max(0, t)
            cr, cb = min(w, rb), min(h, bb)
            if (cl, ct, cr, cb) != (l, t, rb, bb):
                clamped += 1
            crops.append(img.crop((cl, ct, cr, cb)))
            rects.append((cl, ct, cr, cb))
    stats = {"cell": cell, "half": half, "clamped": clamped}
    return crops, rects, stats


# --------------------------------------------------------------------------
# 分割线预览图
# --------------------------------------------------------------------------

def draw_overlay(img, box, bounds_x, bounds_y, n=GRID_N):
    """在原图副本上画分割线，返回新图像（用于预览/导出）。

    画三层：
      1. 15x15 交点网格线（细，橙色）——即棋盘真实网格；
      2. 裁剪边界线（亮青色）——相对网格外扩半格，225 个小图的边界；
      3. 用户拉框本身（黄色粗线）。
    """
    out = img.convert("RGB")
    d = ImageDraw.Draw(out)
    x0, y0, x1, y1 = box
    cell = (x1 - x0) / (n - 1)
    # 1) 交点网格
    thin = max(1, round(cell / 40))
    for k in range(n):
        x = x0 + k * cell
        d.line([(x, y0), (x, y1)], fill="#ff9d2e", width=thin)
        y = y0 + k * cell
        d.line([(x0, y), (x1, y)], fill="#ff9d2e", width=thin)
    # 2) 裁剪边界
    mid = max(1, round(cell / 16))
    for x in bounds_x:
        d.line([(x, bounds_y[0]), (x, bounds_y[n])], fill="#00d0ff", width=mid)
    for y in bounds_y:
        d.line([(bounds_x[0], y), (bounds_x[n], y)], fill="#00d0ff", width=mid)
    # 3) 拉框
    thick = max(2, round(cell / 12))
    d.rectangle([x0, y0, x1, y1], outline="#ffd400", width=thick)
    return out


# --------------------------------------------------------------------------
# 多图合并参照图包保存
# --------------------------------------------------------------------------

def save_reference_package_multi(out_dir, sources, cat_display=None, n=GRID_N):
    """把多张截图的样本合并保存为一个完整参照图包。

    适用场景：带"最新落子标志"的黑/白棋与普通棋子不会出现在同一张截图上，
    需要同时导入多张截图、各自拉框分割，再把所有样本合并导出。

    参数:
        out_dir: 输出目录（不存在则创建）。
        sources: 每张截图一个 dict：
            {
              "name":     文件名（仅记录）,
              "img_size": (w, h),
              "box":      (x0, y0, x1, y1),
              "stats":    crop_intersections 返回的 stats,
              "crops":    crop_intersections 返回的小图列表,
              "selected": {"empty": [(r, c), ...], "black": [...], "white": [...]},
            }
        cat_display: {"empty": "空交点", "black": "黑棋", "white": "白棋"}
                     类目内部键 -> 显示名的映射。
    返回:
        meta 字典（同时写入 out_dir/meta.json）。

    目录结构:
        out_dir/
          empty/   -> 00_00_00.png ...   （图序号_行_列，防跨图同名覆盖）
          black/   -> ...
          white/   -> ...
          meta.json
    """
    if cat_display is None:
        cat_display = {"empty": "empty", "black": "black", "white": "white"}
    os.makedirs(out_dir, exist_ok=True)
    meta_cats = {k: {"display": cat_display.get(k, k), "files": []}
                 for k in cat_display}
    sources_meta = []
    for idx, s in enumerate(sources):
        sel_files = {k: [] for k in cat_display}
        for k in cat_display:
            d = os.path.join(out_dir, k)
            os.makedirs(d, exist_ok=True)
            for (r, c) in sorted(s["selected"].get(k, [])):
                name = f"{idx:02d}_{r:02d}_{c:02d}.png"
                s["crops"][r * n + c].save(os.path.join(d, name))
                meta_cats[k]["files"].append(name)
                sel_files[k].append(name)
        sources_meta.append({
            "name": s["name"],
            "image_size": list(s["img_size"]),
            "box": list(s["box"]),
            "cell": s["stats"]["cell"],
            "half": s["stats"]["half"],
            "clamped_out_of_bounds": s["stats"]["clamped"],
            "selected": sel_files,
        })
    meta = {
        "version": 2,
        "grid_n": n,
        "source_count": len(sources),
        "sources": sources_meta,
        "categories": meta_cats,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


# --------------------------------------------------------------------------
# 参照图包保存
# --------------------------------------------------------------------------

def save_reference_package(out_dir, source_name, img_size, box, stats,
                           crops, categories, n=GRID_N):
    """保存参照图包，供第二部分自动识别软件调用。

    参数:
        out_dir:    输出目录（不存在则创建）。
        source_name: 源图片文件名（仅记录）。
        img_size:    (w, h) 原图尺寸。
        box:         正方形拉框 (x0, y0, x1, y1)。
        stats:       crop_intersections 返回的 stats。
        crops:       crop_intersections 返回的小图列表。
        categories:  {"empty": {"display": "空交点", "keys": [(r, c), ...]}, ...}
                     每个类目下选中的交点 (行, 列) 集合。
    返回:
        meta 字典（同时写入 out_dir/meta.json）。
    目录结构:
        out_dir/
          empty/    -> 00_00.png ...
          black/    -> 03_03.png ...
          white/    -> 05_05.png ...
          meta.json
    """
    os.makedirs(out_dir, exist_ok=True)
    meta_cats = {}
    for key, cat in categories.items():
        d = os.path.join(out_dir, key)
        os.makedirs(d, exist_ok=True)
        files = []
        for (r, c) in sorted(cat["keys"]):
            idx = r * n + c
            name = f"{r:02d}_{c:02d}.png"
            crops[idx].save(os.path.join(d, name))
            files.append(name)
        meta_cats[key] = {"display": cat["display"], "files": files}

    meta = {
        "version": 1,
        "grid_n": n,
        "source_name": source_name,
        "image_size": list(img_size),
        "box": list(box),
        "cell": stats["cell"],
        "half": stats["half"],
        "clamped_out_of_bounds": stats["clamped"],
        "categories": meta_cats,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta
