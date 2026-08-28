from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np

from .engine import BLACK, EMPTY, WHITE
from .models import BoardGeometry


class DetectionError(RuntimeError):
    pass


def _dedupe_rectangles(rectangles: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    result: list[tuple[int, int, int, int]] = []
    for rect in sorted(rectangles, key=lambda r: r[2] * r[3], reverse=True):
        x, y, w, h = rect
        keep = True
        for ox, oy, ow, oh in result:
            ix1, iy1 = max(x, ox), max(y, oy)
            ix2, iy2 = min(x + w, ox + ow), min(y + h, oy + oh)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            union = w * h + ow * oh - inter
            if union and inter / union > 0.82:
                keep = False
                break
        if keep:
            result.append(rect)
    return result[:20]


def _candidate_rectangles(image_rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    height, width = image_rgb.shape[:2]
    min_area = height * width * 0.012
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    scale = min(1.0, 1400.0 / max(height, width))
    if scale < 1:
        small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    else:
        small = gray

    block = max(15, int(round(min(small.shape[:2]) / 45)) | 1)
    binary = cv2.adaptiveThreshold(
        small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 5
    )
    line_len = max(12, int(min(small.shape[:2]) / 65))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, line_len), np.uint8))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((line_len, 1), np.uint8))
    grid = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((5, 5), np.uint8), iterations=1)

    rectangles: list[tuple[int, int, int, int]] = []
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        sx, sy, sw, sh = cv2.boundingRect(contour)
        x, y, w, h = (int(round(v / scale)) for v in (sx, sy, sw, sh))
        if w * h < min_area or not 0.68 <= w / max(h, 1) <= 1.47:
            continue
        pad = int(round(max(w, h) * 0.07))
        rectangles.append((max(0, x - pad), max(0, y - pad), min(width - x + pad, w + 2 * pad), min(height - y + pad, h + 2 * pad)))

    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    warm = cv2.inRange(hsv, np.array([4, 20, 45]), np.array([48, 255, 255]))
    kernel_size = max(9, int(min(height, width) / 90))
    warm = cv2.morphologyEx(warm, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8))
    contours, _ = cv2.findContours(warm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        fill = cv2.contourArea(contour) / max(w * h, 1)
        if w * h >= min_area and fill > 0.55 and 0.68 <= w / max(h, 1) <= 1.47:
            rectangles.append((x, y, w, h))

    # A broad warm mask often joins the board to a dark wood-themed game
    # window.  The brighter playing surface remains a separate, dense region,
    # which gives us a useful candidate on panoramic multi-monitor captures.
    bright_warm = cv2.inRange(hsv, np.array([4, 20, 135]), np.array([48, 255, 255]))
    surface_kernel = max(5, int(min(height, width) / 180))
    bright_warm = cv2.morphologyEx(
        bright_warm,
        cv2.MORPH_CLOSE,
        np.ones((surface_kernel, surface_kernel), np.uint8),
    )
    bright_warm = cv2.morphologyEx(
        bright_warm, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
    )
    contours, _ = cv2.findContours(bright_warm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        fill = cv2.contourArea(contour) / max(w * h, 1)
        if w * h >= min_area and fill > 0.72 and 0.68 <= w / max(h, 1) <= 1.47:
            rectangles.append((x, y, w, h))

    return _dedupe_rectangles(rectangles)


def _axis_projection(gray: np.ndarray, axis: int) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(2.0, min(gray.shape) / 120))
    dark_detail = np.maximum(blur.astype(np.float32) - gray.astype(np.float32), 0)
    if axis == 0:
        return np.mean(dark_detail, axis=0)
    return np.mean(dark_detail, axis=1)


def _fit_axis(projection: np.ndarray, line_count: int) -> tuple[np.ndarray, float]:
    length = len(projection)
    if length < line_count * 3:
        raise DetectionError("候选区域太小，无法拟合棋盘网格")
    best_positions: np.ndarray | None = None
    best_score = -1.0
    radius = max(1, int(length / line_count / 7))
    baseline = float(np.percentile(projection, 55))
    spread = max(float(np.percentile(projection, 95) - baseline), 0.5)
    for start_fraction in np.linspace(0.015, 0.13, 10):
        for end_fraction in np.linspace(0.87, 0.985, 10):
            positions = np.linspace(start_fraction * (length - 1), end_fraction * (length - 1), line_count)
            spacing = float(positions[1] - positions[0])
            if spacing < 4:
                continue
            peaks = []
            adjusted = []
            for position in positions:
                center = int(round(position))
                lo, hi = max(0, center - radius), min(length, center + radius + 1)
                local = projection[lo:hi]
                offset = int(np.argmax(local))
                adjusted.append(lo + offset)
                peaks.append(float(local[offset]))
            deviations = np.abs(np.asarray(adjusted, dtype=float) - positions)
            regularity = max(0.0, 1.0 - float(np.mean(deviations)) / max(radius + 0.5, 1))
            peak_strength = float(np.median((np.asarray(peaks) - baseline) / spread))
            score = 0.78 * peak_strength + 0.22 * regularity
            if score > best_score:
                best_score = score
                best_positions = np.asarray(adjusted, dtype=float)
    if best_positions is None:
        raise DetectionError("没有找到等距网格")
    return best_positions, float(np.clip(best_score, 0.0, 1.0))


def _axis_regularity(lines: np.ndarray) -> float:
    spacings = np.diff(lines)
    if len(spacings) == 0 or np.any(spacings <= 0):
        return 0.0
    mean_spacing = float(np.mean(spacings))
    if mean_spacing <= 0:
        return 0.0
    ideal = np.linspace(float(lines[0]), float(lines[-1]), len(lines))
    residual = float(np.sqrt(np.mean(np.square(lines - ideal))) / mean_spacing)
    spacing_cv = float(np.std(spacings) / mean_spacing)
    return float(np.clip(1.0 - max(residual / 0.20, spacing_cv / 0.28), 0.0, 1.0))


def _grid_metrics(
    x_lines: np.ndarray,
    y_lines: np.ndarray,
    candidate_width: int,
    candidate_height: int,
) -> tuple[float, float, float]:
    x_span = float(x_lines[-1] - x_lines[0])
    y_span = float(y_lines[-1] - y_lines[0])
    if x_span <= 0 or y_span <= 0:
        return 0.0, 0.0, 0.0
    grid_aspect = min(x_span / y_span, y_span / x_span)
    regularity = min(_axis_regularity(x_lines), _axis_regularity(y_lines))
    extent = min(
        x_span / max(candidate_width - 1, 1),
        y_span / max(candidate_height - 1, 1),
    )
    return (
        float(np.clip(grid_aspect, 0.0, 1.0)),
        float(np.clip(regularity, 0.0, 1.0)),
        float(np.clip(extent, 0.0, 1.0)),
    )


def geometry_from_rect(
    image_rgb: np.ndarray,
    rect: tuple[int, int, int, int],
    line_count: int,
    *,
    source: str = "manual",
) -> BoardGeometry:
    x1, y1, x2, y2 = rect
    x1, x2 = sorted((max(0, x1), min(image_rgb.shape[1] - 1, x2)))
    y1, y2 = sorted((max(0, y1), min(image_rgb.shape[0] - 1, y2)))
    if x2 - x1 < line_count * 4 or y2 - y1 < line_count * 4:
        raise DetectionError("框选区域过小")
    gray = cv2.cvtColor(image_rgb[y1 : y2 + 1, x1 : x2 + 1], cv2.COLOR_RGB2GRAY)
    xs, x_score = _fit_axis(_axis_projection(gray, 0), line_count)
    ys, y_score = _fit_axis(_axis_projection(gray, 1), line_count)
    candidate_width = x2 - x1 + 1
    candidate_height = y2 - y1 + 1
    rect_aspect = min(
        candidate_width / max(candidate_height, 1),
        candidate_height / max(candidate_width, 1),
    )
    grid_aspect, regularity, _ = _grid_metrics(
        xs, ys, candidate_width, candidate_height
    )
    confidence = float(
        np.clip(
            0.50 * min(x_score, y_score)
            + 0.25 * grid_aspect
            + 0.15 * rect_aspect
            + 0.10 * regularity,
            0,
            1,
        )
    )
    return BoardGeometry(xs + x1, ys + y1, confidence, source)


def locate_board(image_rgb: np.ndarray, line_count: int = 15) -> BoardGeometry:
    if line_count not in (15, 19):
        raise ValueError("目前仅支持 15 路或 19 路棋盘")
    candidates = _candidate_rectangles(image_rgb)
    if not candidates:
        raise DetectionError("屏幕中没有找到近似方形的棋盘网格")

    best: BoardGeometry | None = None
    best_score = -1.0
    for x, y, w, h in candidates:
        try:
            geometry = geometry_from_rect(image_rgb, (x, y, x + w - 1, y + h - 1), line_count, source="auto")
        except DetectionError:
            continue
        grid_aspect, regularity, extent = _grid_metrics(
            geometry.x_lines - x, geometry.y_lines - y, w, h
        )
        if grid_aspect < 0.84 or regularity < 0.42:
            continue
        score = 0.70 * geometry.confidence + 0.20 * regularity + 0.10 * extent
        if score > best_score:
            best_score = score
            best = replace(geometry, confidence=float(np.clip(score, 0, 1)))
    if best is None or best_score < 0.72:
        raise DetectionError("发现了候选区域，但网格间距不稳定")
    return best


def with_screen_origin(geometry: BoardGeometry, origin: tuple[int, int]) -> BoardGeometry:
    return replace(geometry, screen_left=origin[0], screen_top=origin[1])


def _axis_liveness(
    gray: np.ndarray,
    lines: np.ndarray,
    start: int,
    end: int,
    spacing: float,
    *,
    vertical: bool,
) -> float:
    """Measure whether a set of locked, equally spaced lines is still visible.

    A board can change screens without changing the captured window dimensions.
    In that case the old coordinates must not be trusted just because every
    stone patch looks like an empty, high-confidence patch.  We compare each
    expected line with flanks half a cell away and permit only a tiny capture
    jitter when choosing the strongest nearby pixel column/row.
    """

    if len(lines) < 5 or spacing < 4 or start > end:
        return 0.0
    height, width = gray.shape
    half = max(0, min(1, int(round(spacing * 0.035))))
    flank_offset = max(8, int(round(spacing * 0.42)))
    search_radius = max(1, min(6, int(round(spacing * 0.16))))
    contrasts: list[float] = []
    offsets: list[int] = []

    for line in lines:
        expected = int(round(float(line)))
        best_contrast = -1.0
        best_offset: int | None = None
        for offset in range(-search_radius, search_radius + 1):
            point = expected + offset
            if vertical:
                if (
                    point - half < 0
                    or point + half >= width
                    or point - flank_offset - half < 0
                    or point + flank_offset + half >= width
                ):
                    continue
                core = gray[start : end + 1, point - half : point + half + 1]
                left = gray[start : end + 1, point - flank_offset - half : point - flank_offset + half + 1]
                right = gray[start : end + 1, point + flank_offset - half : point + flank_offset + half + 1]
            else:
                if (
                    point - half < 0
                    or point + half >= height
                    or point - flank_offset - half < 0
                    or point + flank_offset + half >= height
                ):
                    continue
                core = gray[point - half : point + half + 1, start : end + 1]
                left = gray[point - flank_offset - half : point - flank_offset + half + 1, start : end + 1]
                right = gray[point + flank_offset - half : point + flank_offset + half + 1, start : end + 1]
            flank = np.concatenate((left.reshape(-1), right.reshape(-1)))
            contrast = abs(float(np.median(flank)) - float(np.median(core)))
            # Prefer the locked coordinate when two nearby samples are tied.
            if contrast > best_contrast + 1e-6 or (
                abs(contrast - best_contrast) <= 1e-6
                and (best_offset is None or abs(offset) < abs(best_offset))
            ):
                best_contrast = contrast
                best_offset = offset
        if best_offset is not None:
            contrasts.append(best_contrast)
            offsets.append(best_offset)

    if len(contrasts) < max(5, int(round(len(lines) * 0.8))):
        return 0.0
    contrast_array = np.asarray(contrasts, dtype=np.float32)
    # The two border lines include a frame/shadow whose strongest edge can be
    # several pixels away from the mathematical intersection.  They still
    # contribute to the visibility signal, but must not make an otherwise
    # aligned board look displaced.
    offset_values = offsets[1:-1] if len(offsets) > 4 else offsets
    offset_array = np.abs(np.asarray(offset_values, dtype=np.float32))
    # The absolute contrast keeps this check useful for light-on-dark boards;
    # requiring most lines and aligned peaks prevents a single stone or icon
    # from satisfying the liveness test.
    strength = float(np.clip((np.median(contrast_array) - 8.0) / 24.0, 0.0, 1.0))
    hit_ratio = float(np.mean(contrast_array >= 12.0))
    alignment = float(
        np.clip(
            1.0
            - max(
                float(np.median(offset_array)) / 4.0,
                float(np.percentile(offset_array, 90)) / 6.0,
            ),
            0.0,
            1.0,
        )
    )
    return float(np.clip((0.70 * strength + 0.30 * hit_ratio) * alignment, 0.0, 1.0))


def grid_liveness(image_rgb: np.ndarray, geometry: BoardGeometry) -> float:
    """Return a 0..1 score indicating whether ``geometry`` fits this frame.

    This is intentionally separate from :func:`read_stones`: an empty patch
    on a menu or a dialog can have perfect stone confidence, but it is not a
    live chessboard.  Callers that may click should require this score in
    addition to stone confidence.
    """

    image = np.asarray(image_rgb)
    if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
        return 0.0
    try:
        x_lines = np.asarray(geometry.x_lines, dtype=np.float32)
        y_lines = np.asarray(geometry.y_lines, dtype=np.float32)
        spacing = float(geometry.spacing)
    except (AttributeError, TypeError, ValueError):
        return 0.0
    if len(x_lines) != len(y_lines) or len(x_lines) < 5 or not np.isfinite(spacing):
        return 0.0
    height, width = image.shape[:2]
    x_start, x_end = int(round(float(x_lines[0]))), int(round(float(x_lines[-1])))
    y_start, y_end = int(round(float(y_lines[0]))), int(round(float(y_lines[-1])))
    if not (0 <= x_start < x_end < width and 0 <= y_start < y_end < height):
        return 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    x_score = _axis_liveness(
        gray,
        x_lines,
        y_start,
        y_end,
        spacing,
        vertical=True,
    )
    y_score = _axis_liveness(
        gray,
        y_lines,
        x_start,
        x_end,
        spacing,
        vertical=False,
    )
    return float(min(x_score, y_score))


def read_stones(image_rgb: np.ndarray, geometry: BoardGeometry) -> tuple[np.ndarray, np.ndarray]:
    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    board = np.full((geometry.size, geometry.size), EMPTY, dtype=np.int8)
    confidence = np.zeros_like(board, dtype=np.float32)
    radius = max(3, int(round(geometry.spacing * 0.30)))
    outer_radius = max(radius + 2, int(round(geometry.spacing * 0.47)))

    yy, xx = np.ogrid[-outer_radius : outer_radius + 1, -outer_radius : outer_radius + 1]
    distance = np.sqrt(xx * xx + yy * yy)
    inner_mask = distance <= radius
    ring_mask = (distance >= radius * 1.16) & (distance <= outer_radius)

    for row, yf in enumerate(geometry.y_lines):
        for col, xf in enumerate(geometry.x_lines):
            x, y = int(round(float(xf))), int(round(float(yf)))
            x1, x2 = x - outer_radius, x + outer_radius + 1
            y1, y2 = y - outer_radius, y + outer_radius + 1
            if x1 < 0 or y1 < 0 or x2 > gray.shape[1] or y2 > gray.shape[0]:
                continue
            patch_gray = gray[y1:y2, x1:x2]
            patch_hsv = hsv[y1:y2, x1:x2]
            inner = patch_gray[inner_mask].astype(np.float32)
            ring = patch_gray[ring_mask].astype(np.float32)
            inner_mean, ring_mean = float(np.mean(inner)), float(np.mean(ring))
            dark_ratio = float(np.mean(inner < 92))
            bright_neutral = (patch_gray > 215) & (patch_hsv[:, :, 1] < 70)
            white_ratio = float(np.mean(bright_neutral[inner_mask]))
            red_marker = (
                ((patch_hsv[:, :, 0] <= 8) | (patch_hsv[:, :, 0] >= 170))
                & (patch_hsv[:, :, 1] > 100)
                & (patch_hsv[:, :, 2] > 105)
            )
            red_marker_ratio = float(np.mean(red_marker[inner_mask]))

            black_strength = max((ring_mean - inner_mean - 25) / 75, (dark_ratio - 0.26) / 0.58)
            white_strength = max((inner_mean - ring_mean - 18) / 62, (white_ratio - 0.36) / 0.58)
            if inner_mean - ring_mean >= 20 and white_ratio >= 0.45 and dark_ratio < 0.12:
                plain_white = 0.58 + min(0.38, (inner_mean - ring_mean - 20) / 55)
                white_strength = max(white_strength, plain_white)
            # Some clients draw a red cross over the latest white stone. The
            # cross lowers the pure-white ratio, so combine its distinctive
            # red area with the stone's positive center-to-ring contrast.
            if (
                red_marker_ratio >= 0.075
                and inner_mean - ring_mean >= 5
                and white_ratio >= 0.22
                and dark_ratio < 0.12
            ):
                marked_white = 0.54 + min(0.40, (red_marker_ratio - 0.075) * 2.5)
                white_strength = max(white_strength, marked_white)
            if black_strength >= 0.43 and black_strength > white_strength + 0.08:
                board[row, col] = BLACK
                confidence[row, col] = float(np.clip(black_strength, 0, 1))
            elif white_strength >= 0.43 and white_strength > black_strength + 0.08:
                board[row, col] = WHITE
                confidence[row, col] = float(np.clip(white_strength, 0, 1))
            else:
                confidence[row, col] = float(np.clip(1 - max(black_strength, white_strength), 0, 1))
    return board, confidence
