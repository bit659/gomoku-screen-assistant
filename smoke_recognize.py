"""识别算法冒烟：合成棋盘图，检查两种识别方式的边缘裁剪与置信度口径。

覆盖的问题（2026-11-06 实测定位）：
- 棋盘贴着窗口边缘时，最外圈交点的裁剪框越过图像边界。旧实现遇到越界
  直接跳过，这些交点的置信度停在初始值 0，"最低交点置信度"整盘被判 0，
  识别完全正确也会被门槛拒绝落子/计算。
- 补齐填充若直接复制最近边缘像素，当网格线压在边界上会把半个格子涂成
  线色，凭空造出深色块，导致判读出错；改用可见区中位色填充。

本脚本只用合成的木纹棋盘 + 合成模板，不依赖真实截图和参照图包。
运行：python smoke_recognize.py   （EXIT=0 通过，失败时逐项列出期望/实际）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from gomoku_assistant.detector import read_stones
from gomoku_assistant.engine import BLACK, EMPTY, WHITE
from gomoku_assistant.models import BoardGeometry
from gomoku_assistant.template_matcher import TemplateLibrary, read_board_template

WOOD = np.array([222, 190, 140], np.uint8)
LINE = (120, 90, 60)
SPACING = 48
GRID = 15
# 滑块默认 0.55，棋子门槛 = 0.55 × 0.6
STONE_THRESHOLD = round(0.55 * 0.6, 2)
STONES = {(3, 3): BLACK, (5, 7): WHITE, (9, 10): BLACK, (12, 6): WHITE}

FAILURES: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{name}：期望 {expected}，实际 {actual}")


def _template_library() -> TemplateLibrary:
    lib = TemplateLibrary(dir_path="<synthetic>")

    def empty(size: int = 44) -> np.ndarray:
        img = np.tile(WOOD, (size, size, 1))
        cv2.line(img, (0, size // 2), (size - 1, size // 2), LINE, 2)
        cv2.line(img, (size // 2, 0), (size // 2, size - 1), LINE, 2)
        return img

    def stone(gray: int, size: int = 44, radius: int = 16) -> np.ndarray:
        img = np.tile(WOOD, (size, size, 1))
        cv2.circle(img, (size // 2, size // 2), radius, (gray, gray, gray), -1)
        rim = (200, 200, 200) if gray > 128 else (80, 80, 80)
        cv2.circle(img, (size // 2, size // 2), radius, rim, 1)
        return img

    lib.colors = {"empty": [empty()], "black": [stone(25)], "white": [stone(250)]}
    lib.templates = {k: [cv2.cvtColor(v[0], cv2.COLOR_RGB2GRAY)] for k, v in lib.colors.items()}
    lib.names = {"empty": ["empty.png"], "black": ["black.png"], "white": ["white.png"]}
    lib.display_names = {"empty": "空交点", "black": "黑棋", "white": "白棋"}
    return lib


def _scene(margin: int) -> tuple[np.ndarray, BoardGeometry]:
    xs = np.array([margin + i * SPACING for i in range(GRID)], dtype=np.float64)
    ys = xs.copy()
    size = int(margin + (GRID - 1) * SPACING + margin)
    img = np.tile(WOOD, (size, size, 1))
    for x in xs:
        cv2.line(img, (int(x), 0), (int(x), size - 1), LINE, 2)
    for y in ys:
        cv2.line(img, (0, int(y)), (size - 1, int(y)), LINE, 2)
    for (row, col), label in STONES.items():
        gray = 25 if label == BLACK else 250
        cv2.circle(img, (int(xs[col]), int(ys[row])), 16, (gray, gray, gray), -1)
    return img, BoardGeometry(xs, ys, 0.99, "manual")


def _judgement_ok(board: np.ndarray) -> bool:
    for (row, col), label in STONES.items():
        if int(board[row, col]) != label:
            return False
    extra = [tuple(map(int, rc)) for rc in np.argwhere(board != EMPTY) if tuple(map(int, rc)) not in STONES]
    return not extra


def main() -> int:
    lib = _template_library()
    for margin in (0, 1, 2, 8, 20):
        img, geometry = _scene(margin)
        board_t, conf_t = read_board_template(img, geometry, lib)
        board_c, conf_c = read_stones(img, geometry)
        check(f"margin={margin} 模板匹配判读正确", _judgement_ok(board_t), True)
        check(f"margin={margin} 颜色阈值判读正确", _judgement_ok(board_c), True)
        check(f"margin={margin} 模板匹配无零置信度格", int((conf_t == 0.0).sum()), 0)
        check(f"margin={margin} 颜色阈值无零置信度格", int((conf_c == 0.0).sum()), 0)
        check(f"margin={margin} 模板匹配最低置信度过门槛", bool(conf_t.min() >= STONE_THRESHOLD), True)
        check(f"margin={margin} 颜色阈值最低置信度过门槛", bool(conf_c.min() >= STONE_THRESHOLD), True)

    img, geometry = _scene(8)
    first = read_board_template(img, geometry, lib)
    read_stones(img, geometry)
    second = read_board_template(img, geometry, lib)
    same = bool(np.array_equal(first[0], second[0]) and np.allclose(first[1], second[1]))
    check("交替调用后模板匹配结果一致", same, True)

    if FAILURES:
        print(f"识别冒烟失败（{len(FAILURES)} 项）：")
        for item in FAILURES:
            print("  - " + item)
        return 1
    print("识别冒烟 OK（5 种贴边布局 × 模板匹配/颜色阈值：判读、零值格、门槛）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
