# -*- coding: utf-8 -*-
"""生成示例输出：合成一张五子棋截图，跑完整分割 + 分类 + 图包保存。

输出到 示例输出/ 目录：
  1. 合成截图.png              —— 模拟的小程序截图
  2. 分割线预览_示例.png        —— 带分割线的预览图（可直接肉眼核对 225 交点）
  3. 示例参照图包/              —— empty/black/white + meta.json
"""
import os

from PIL import Image, ImageDraw

import crop_math

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "示例输出")
os.makedirs(OUT, exist_ok=True)

SIZE = 1000
LEFT, TOP, RIGHT, BOTTOM = 150, 150, 850, 850  # 棋盘外沿
N = 15
CELL = (RIGHT - LEFT) / (N - 1)  # 50px


def make_board_image():
    img = Image.new("RGB", (SIZE, SIZE), (250, 246, 238))
    d = ImageDraw.Draw(img)
    # 背景留白区域画点装饰
    # 棋盘网格（木色）
    for k in range(N):
        x = LEFT + k * CELL
        d.line([(x, TOP), (x, BOTTOM)], fill=(150, 110, 60), width=2)
        y = TOP + k * CELL
        d.line([(LEFT, y), (RIGHT, y)], fill=(150, 110, 60), width=2)
    # 星位
    for (sr, sc) in [(3, 3), (3, 11), (7, 7), (11, 3), (11, 11)]:
        x, y = LEFT + sc * CELL, TOP + sr * CELL
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(90, 60, 30))
    # 棋子
    def stone(r, c, color, radius=19):
        x, y = LEFT + c * CELL, TOP + r * CELL
        d.ellipse([x - radius, y - radius, x + radius, y + radius],
                  fill=color, outline=(40, 40, 40))
    stone(3, 3, (30, 30, 30))      # 黑
    stone(7, 7, (30, 30, 30))      # 黑（带最新落子标记）
    stone(11, 3, (30, 30, 30))     # 黑（边角）
    stone(3, 11, (245, 245, 245))  # 白
    stone(9, 5, (245, 245, 245))   # 白
    # 最新落子标记：红色小三角
    x, y = LEFT + 7 * CELL, TOP + 7 * CELL
    d.polygon([(x, y - 24), (x - 8, y - 12), (x + 8, y - 12)], fill=(220, 40, 40))
    return img


def main():
    img = make_board_image()
    img.save(os.path.join(OUT, "合成截图.png"))
    box = (LEFT, TOP, RIGHT, BOTTOM)

    # 分割 + 预览
    crops, rects, stats = crop_math.crop_intersections(img, box)
    bounds_x, bounds_y, _, _ = crop_math.compute_crop_grid(box)
    preview = crop_math.draw_overlay(img, box, bounds_x, bounds_y)
    preview.save(os.path.join(OUT, "分割线预览_示例.png"))

    # 人工分类模拟
    categories = {
        "empty": {"display": "空交点",
                  "keys": [(0, 0), (0, 1), (1, 0), (5, 5), (14, 14)]},
        "black": {"display": "黑棋",
                  "keys": [(3, 3), (7, 7), (11, 3)]},
        "white": {"display": "白棋",
                  "keys": [(3, 11), (9, 5)]},
    }
    pkg = os.path.join(OUT, "示例参照图包")
    crop_math.save_reference_package(
        out_dir=pkg, source_name="合成截图.png", img_size=img.size,
        box=box, stats=stats, crops=crops, categories=categories)

    print("示例输出目录:", OUT)
    print("crops:", len(crops), "clamped:", stats["clamped"],
          "cell:", round(stats["cell"], 2))


if __name__ == "__main__":
    main()
