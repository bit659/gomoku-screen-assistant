"""OpenCV 多模板匹配识别模块。

第二部分软件的主识别路径：用第一部分导出的“参照图包”（meta.json v2 +
empty/black/white 子目录）作为模板库，对目标窗口棋盘每个交点裁出小图，
与三类模板做多模板匹配（TM_CCOEFF_NORMED）分类。

降分辨率规则（用户明确要求）：
- 对比“别处棋盘小截图”与“模板小截图”时，谁分辨率高就降谁到另一个的
  分辨率，再匹配；任何一方都禁止被放大（禁止超分辨率）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from .detector import crop_cell
from .engine import BLACK, EMPTY, WHITE

# 模板类目键 -> 棋盘状态常量（与 engine.py 一致）
CATEGORY_LABELS = {"empty": EMPTY, "black": BLACK, "white": WHITE}

_META_FALLBACK_DISPLAY = {"empty": "空交点", "black": "黑棋", "white": "白棋"}


def make_square_box(anchor: tuple[float, float], current: tuple[float, float]) -> tuple[int, int, int, int] | None:
    """拉框时强制正方形（与第一部分 crop_math.make_square_box 同规则）。

    以主轴（|dx|、|dy| 较大者）决定边长，另一轴跟随主轴保持正方形；
    返回规范化矩形 (x0, y0, x1, y1)，x1>x0、y1>y0；原地不动返回 None。
    """
    ax, ay = anchor
    cx, cy = current
    dx, dy = cx - ax, cy - ay
    side = max(abs(dx), abs(dy))
    if side == 0:
        return None
    sx = 1 if dx >= 0 else -1
    sy = 1 if dy >= 0 else -1
    if dx == 0:
        sx = sy
    if dy == 0:
        sy = sx
    x0, x1 = (ax, ax + side * sx)
    y0, y1 = (ay, ay + side * sy)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _read_image(path: str) -> np.ndarray:
    """读取图片为 RGB 数组；失败抛 ValueError。

    这里不用 cv2.imread：它在 Windows 上不认非 ASCII 路径，图包放在含中文的
    文件夹里会直接返回 None（表现为“无法读取模板图片”）。改成用内建 open
    读字节再交给 cv2.imdecode，路径解析走 Python，中文/空格都正常。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ValueError(f"无法读取模板图片：{path}（{exc}）") from exc
    if not data:
        raise ValueError(f"无法读取模板图片：{path}（文件为空）")
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取模板图片：{path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


@dataclass
class TemplateLibrary:
    """加载后的模板库：每类目一组模板（灰度用于匹配，RGB 用于展示）。"""

    dir_path: str
    templates: dict[str, list[np.ndarray]] = field(default_factory=dict)
    colors: dict[str, list[np.ndarray]] = field(default_factory=dict)
    names: dict[str, list[str]] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)

    def categories(self) -> list[str]:
        return list(self.templates.keys())

    def summary(self) -> str:
        parts = [f"{self.display_names.get(k, k)} {len(self.templates[k])} 张"
                 for k in self.categories()]
        return "，".join(parts)

    def _category_best(self, patch_gray: np.ndarray, key: str, pad: int = 0) -> tuple[float, str]:
        """该类目下与 patch 最相似模板的 (得分, 文件名)。"""
        best_score = -2.0
        best_name = ""
        for tpl, name in zip(self.templates[key], self.names[key]):
            score = match_similarity(tpl, patch_gray, pad=pad)
            if score > best_score:
                best_score, best_name = score, name
        return best_score, best_name

    def _match_resolution(self, template: np.ndarray, patch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """把两张图降到相同分辨率：只降大的一方，绝不放大（禁止超分辨率）。"""
        if template.ndim == 3:
            template = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
        if patch.ndim == 3:
            patch = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
        if template.shape[:2] == patch.shape[:2]:
            return template, patch
        target = (min(template.shape[0], patch.shape[0]),
                  min(template.shape[1], patch.shape[1]))
        t = template
        if t.shape[:2] != target:
            t = cv2.resize(t, (target[1], target[0]), interpolation=cv2.INTER_AREA)
        p = patch
        if p.shape[:2] != target:
            p = cv2.resize(p, (target[1], target[0]), interpolation=cv2.INTER_AREA)
        return t, p


def load_template_package(folder: str) -> TemplateLibrary:
    """加载参照图包目录。

    优先读取第一部分导出的 meta.json（version 2）确定类目与文件清单；
    无 meta.json 时回退为扫描 empty/black/white 子目录下的图片。
    至少需要存在一个可识别类目，否则抛 ValueError。
    """
    if not os.path.isdir(folder):
        raise ValueError(f"模板文件夹不存在：{folder}")
    lib = TemplateLibrary(dir_path=folder)
    meta = _load_meta(folder)
    if meta is not None:
        for key, info in meta.get("categories", {}).items():
            if key not in CATEGORY_LABELS:
                continue
            files = [f for f in info.get("files", []) if f]
            if not files:
                continue
            _fill_category(lib, folder, key, files)
            lib.display_names[key] = str(info.get("display") or key)
    if not lib.categories():
        # 回退：扫描标准子目录
        for key in CATEGORY_LABELS:
            sub = os.path.join(folder, key)
            if not os.path.isdir(sub):
                continue
            files = sorted(
                f for f in os.listdir(sub)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp"))
            )
            if files:
                _fill_category(lib, folder, key, files)
                lib.display_names[key] = _META_FALLBACK_DISPLAY.get(key, key)
    if not lib.categories():
        raise ValueError(
            "模板文件夹中未找到可用类目（需要 empty/black/white 子目录或其 meta.json）"
        )
    for key in CATEGORY_LABELS:
        lib.display_names.setdefault(key, _META_FALLBACK_DISPLAY.get(key, key))
    return lib


def _load_meta(folder: str) -> dict | None:
    path = os.path.join(folder, "meta.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except (ValueError, OSError):
            return None
    return data if isinstance(data, dict) else None


def _fill_category(lib: TemplateLibrary, folder: str, key: str, files: list[str]) -> None:
    for fname in files:
        path = os.path.join(folder, key, fname)
        if not os.path.isfile(path):
            continue
        rgb = _read_image(path)
        lib.colors.setdefault(key, []).append(rgb)
        lib.templates.setdefault(key, []).append(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
        lib.names.setdefault(key, []).append(fname)


def match_similarity(template: np.ndarray, patch: np.ndarray, pad: int = 0) -> float:
    """两图先“仅降分辨率”等化，再计算 TM_CCOEFF_NORMED 相似度。

    等化规则（用户明确要求）：谁分辨率高就降谁到另一个的分辨率，
    任何一方都不放大（禁止超分辨率）。

    pad>0 时在小图周围复制边缘扩充 pad 像素后再做模板匹配并取最大响应，
    用来吸收棋盘定位的 1~2px 相位误差（平坦空交点对亚像素错位极敏感）。
    """
    t, p = _equalize(template, patch)
    if pad > 0:
        p = cv2.copyMakeBorder(p, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    result = cv2.matchTemplate(p.astype(np.float32), t.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    return float(result.max())


def _equalize(template: np.ndarray, patch: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """统一灰度 + 只降不升：返回同分辨率的两张灰度图。"""
    if template.ndim == 3:
        template = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY)
    if patch.ndim == 3:
        patch = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    if template.shape[:2] == patch.shape[:2]:
        return template, patch
    target = (min(template.shape[0], patch.shape[0]),
              min(template.shape[1], patch.shape[1]))
    if template.shape[:2] != target:
        template = cv2.resize(template, (target[1], target[0]), interpolation=cv2.INTER_AREA)
    if patch.shape[:2] != target:
        patch = cv2.resize(patch, (target[1], target[0]), interpolation=cv2.INTER_AREA)
    return template, patch


def classify_patch(patch: np.ndarray, lib: TemplateLibrary) -> tuple[int, float, str]:
    """单张交点小图三分类。

    返回 (状态常量, 最高相似度, 最佳模板文件名)。多模板取类目内最大值，
    跨类目取最高者。
    """
    patch_gray = patch if patch.ndim == 2 else cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    # 平移容差：按小图尺寸的 1/8 取 padding，吸收棋盘网格的亚像素/整数像素相位误差
    pad = max(2, int(round(min(patch_gray.shape) / 8)))
    best_label = EMPTY
    best_score = -2.0
    best_name = ""
    for key, label in CATEGORY_LABELS.items():
        if key not in lib.templates:
            continue
        score, name = lib._category_best(patch_gray, key, pad=pad)
        if score > best_score:
            best_score, best_label, best_name = score, label, name
    return best_label, float(np.clip(best_score, 0.0, 1.0)), best_name


def read_board_template(
    image_rgb: np.ndarray,
    geometry,
    lib: TemplateLibrary,
) -> tuple[np.ndarray, np.ndarray]:
    """用模板匹配读取整盘棋子。

    对每个交点裁出 cell×cell 小图（交点居中，与第一部分裁剪规则一致），
    与模板库匹配分类。返回 (board int8, confidence float32)，
    board 值域与 read_stones 一致（EMPTY/BLACK/WHITE）。
    """
    n = geometry.size
    board = np.full((n, n), EMPTY, dtype=np.int8)
    confidence = np.zeros((n, n), dtype=np.float32)
    cell = geometry.spacing
    half = max(3.0, cell / 2.0)
    for row in range(n):
        yf = float(geometry.y_lines[row])
        y0 = int(round(yf - half))
        y1 = int(round(yf + half))
        for col in range(n):
            xf = float(geometry.x_lines[col])
            x0 = int(round(xf - half))
            x1 = int(round(xf + half))
            patch = crop_cell(image_rgb, x0, y0, x1, y1)
            if patch.shape[0] < 2 or patch.shape[1] < 2:
                continue
            label, score, _ = classify_patch(patch, lib)
            board[row, col] = label
            confidence[row, col] = np.float32(score)
    return board, confidence
