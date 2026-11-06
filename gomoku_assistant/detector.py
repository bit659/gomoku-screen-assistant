from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np

from .engine import BLACK, EMPTY, WHITE
from .models import BoardGeometry


class DetectionError(RuntimeError):
    pass


def crop_cell(image: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """裁取交点小图；框越过图像边界时用可见区中位色补齐。

    棋盘通常贴着窗口边缘，最外圈交点的裁剪框必然越过图像边界。旧逻辑遇到
    越界直接 continue，这些交点的置信度就停在初始值 0，于是"最低交点置信度"
    整盘被判成 0——识别完全正确也会被门槛拒绝。这里先裁可见部分，越界部分
    用"可见像素的中位色"填充，返回尺寸恒为 (y1-y0, x1-x0)，让最外圈也参与判读。

    填充色取中位色而不是复制最近边缘像素：棋盘线正好压在图像边界时，边缘那
    一列本身就是网格线色，复制会把半个格子涂成深色，凭空造出一块"黑棋"。
    """
    h, w = image.shape[:2]
    ys = min(max(y0, 0), h - 1)
    ye = min(max(y1, ys + 1), h)
    xs = min(max(x0, 0), w - 1)
    xe = min(max(x1, xs + 1), w)
    src = image[ys:ye, xs:xe]
    top = max(0, ys - y0)
    left = max(0, xs - x0)
    bottom = max(0, y1 - ye)
    right = max(0, x1 - xe)
    if top or bottom or left or right:
        if src.ndim == 3:
            fill = tuple(int(v) for v in np.median(src.reshape(-1, src.shape[2]), axis=0))
        else:
            fill = int(np.median(src))
        src = cv2.copyMakeBorder(src, top, bottom, left, right, cv2.BORDER_CONSTANT, value=fill)
    return src


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

    # Cool-colored playing surfaces (green / blue felt boards are common in
    # mini-program clients); the warm mask above misses them entirely.
    cool = cv2.inRange(hsv, np.array([40, 25, 45]), np.array([130, 255, 255]))
    cool = cv2.morphologyEx(cool, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8))
    contours, _ = cv2.findContours(cool, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        fill = cv2.contourArea(contour) / max(w * h, 1)
        if w * h >= min_area and fill > 0.55 and 0.68 <= w / max(h, 1) <= 1.47:
            rectangles.append((x, y, w, h))

    # Bright, low-saturation surfaces (ivory / beige boards) also miss both
    # colored masks; a large bright neutral region with grid lines inside is a
    # reliable candidate on its own.
    bright_neutral = cv2.inRange(hsv, np.array([0, 0, 165]), np.array([180, 55, 255]))
    bright_neutral = cv2.morphologyEx(
        bright_neutral, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8)
    )
    contours, _ = cv2.findContours(bright_neutral, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        fill = cv2.contourArea(contour) / max(w * h, 1)
        if w * h >= min_area and fill > 0.60 and 0.68 <= w / max(h, 1) <= 1.47:
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
    # Detect both darker-than-background and lighter-than-background grid lines.
    # Dark boards with light lines (common in mini-program themes) produce no
    # signal under the old dark-only projection and fail the whole fit.
    dark_detail = np.maximum(blur.astype(np.float32) - gray.astype(np.float32), 0)
    light_detail = np.maximum(gray.astype(np.float32) - blur.astype(np.float32), 0)
    detail = np.maximum(dark_detail, light_detail)
    if axis == 0:
        return np.mean(detail, axis=0)
    return np.mean(detail, axis=1)


def _coarse_phase_offset(
    projection: np.ndarray,
    positions: np.ndarray,
    radius: int,
) -> float:
    """Scan a global shift for an equally spaced lattice against the projection.

    The brute-force start/end sweep quantizes the phase; stones and icons can
    also bias a lattice by a fraction of a cell.  A whole-lattice shift that
    maximizes the median peak strength removes that systematic offset.
    """

    length = len(projection)
    spacing = float(np.median(np.diff(positions))) if len(positions) > 1 else 1.0
    if spacing <= 0:
        return 0.0
    baseline = float(np.percentile(projection, 55))
    spread = max(float(np.percentile(projection, 95) - baseline), 0.5)
    peak_threshold = baseline + 0.30 * spread
    best_offset = 0.0
    best_score = -1.0
    for step_fraction in np.linspace(-0.45, 0.45, 19):
        offset = step_fraction * spacing
        peaks = []
        for pos in positions:
            center = int(round(pos + offset))
            lo, hi = max(0, center - radius), min(length, center + radius + 1)
            if hi <= lo:
                peaks.append(0.0)
                continue
            peaks.append(float(np.max(projection[lo:hi])))
        peak_array = np.asarray(peaks, dtype=np.float32)
        # A lattice shifted by a whole period keeps the median peak strength but
        # puts the outer lines in the margin (no real grid line there).  Penalize
        # phases where many lines do not sit on a genuine peak, otherwise the fit
        # can lock onto a phase-shifted lattice that misreads every stone.
        hit_ratio = float(np.mean(peak_array >= peak_threshold))
        strength = float(np.median(peak_array))
        score = strength * (0.30 + 0.70 * hit_ratio)
        if score > best_score:
            best_score = score
            best_offset = offset
    return best_offset


def _subpixel_peak(projection: np.ndarray, center: int, radius: int) -> tuple[float, float]:
    """Locate a peak near ``center`` with subpixel precision (parabolic fit)."""

    length = len(projection)
    lo, hi = max(0, center - radius), min(length, center + radius + 1)
    local = projection[lo:hi]
    if hi <= lo:
        return float(center), 0.0
    peak_index = int(np.argmax(local))
    value = float(local[peak_index])
    if 0 < peak_index < len(local) - 1:
        left, right = float(local[peak_index - 1]), float(local[peak_index + 1])
        denom = left - 2.0 * value + right
        if abs(denom) > 1e-9:
            delta = 0.5 * (left - right) / denom
            peak_index += max(-1.0, min(1.0, delta))
    return lo + peak_index, value


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
            phase_offset = _coarse_phase_offset(projection, positions, radius)
            peaks = []
            adjusted = []
            for position in positions:
                center = int(round(position + phase_offset))
                refined, peak_value = _subpixel_peak(projection, center, radius)
                adjusted.append(refined)
                peaks.append(peak_value)
            deviations = np.abs(np.asarray(adjusted, dtype=float) - (positions + phase_offset))
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


def _find_equidistant_peaks(
    projection: np.ndarray,
    line_count: int,
) -> np.ndarray | None:
    """Find an equally spaced run of strong peaks in a 1-D projection.

    Used as a full-image fallback when color/grid candidates fail: boards that
    fill the capture and share the background hue (common in mini-program
    clients) never produce an isolated rectangle, yet their grid lines still
    form a regular peak train in the projection.
    """

    n = len(projection)
    if n < line_count * 3:
        return None
    proj = projection.astype(np.float64)
    kernel_size = max(1, n // 350)
    if kernel_size > 1:
        kernel = np.ones(kernel_size, dtype=np.float64) / kernel_size
        proj = np.convolve(proj, kernel, mode="same")
    baseline = float(np.percentile(proj, 55))
    spread = max(float(np.percentile(proj, 97) - baseline), 0.5)
    threshold = baseline + 0.35 * spread
    peaks: list[tuple[float, float]] = []
    for i in range(1, n - 1):
        if proj[i] >= proj[i - 1] and proj[i] > proj[i + 1] and proj[i] >= threshold:
            peaks.append((float(i), float(proj[i])))
    if len(peaks) < max(4, int(line_count * 0.6)):
        return None
    positions = np.asarray([p[0] for p in peaks], dtype=np.float64)
    diffs = np.diff(positions)
    if len(diffs) == 0:
        return None
    # The dominant spacing is the most common gap between neighbouring peaks,
    # weighted by gap length: dense clusters of decoration peaks produce many
    # tiny gaps, and without the weighting they would dominate the histogram
    # and hide the true grid spacing.
    hist, edges = np.histogram(diffs, bins=min(40, len(diffs)), weights=diffs)
    spacing = float((edges[int(np.argmax(hist))] + edges[int(np.argmax(hist)) + 1]) / 2)
    if spacing < 4:
        return None

    best_seq: list[float] | None = None
    best_score = -1.0
    peak_values = proj  # smoothed projection, matched positions index into it
    for start in positions:
        seq = [float(start)]
        pos = float(start)
        for _ in range(line_count - 1):
            pos += spacing
            candidates = positions[np.abs(positions - pos) <= spacing * 0.18]
            if len(candidates) == 0:
                break
            nearest = float(candidates[np.argmin(np.abs(candidates - pos))])
            seq.append(nearest)
            pos = nearest
        # Prefer the longest run, and among equally long runs the one with the
        # stronger peak intensities: a decoration can occasionally form an
        # equally spaced train that is just as long but much weaker.
        strength = sum(float(peak_values[int(round(p))]) for p in seq) / len(seq)
        score = len(seq) * 10.0 + strength / 25.5
        if score > best_score:
            best_score = score
            best_seq = seq
    if best_seq is None or len(best_seq) < max(4, int(line_count * 0.7)):
        return None
    return np.asarray(best_seq, dtype=np.float64)


def _line_occupancy_projection(gray: np.ndarray, axis: int) -> np.ndarray:
    """Project a grid-line occupancy signal with strong peaks for thin lines.

    A plain mean-intensity projection dilutes horizontal lines: a 3 px line
    across an 800 px wide board contributes almost nothing to the row mean.
    Morphologically opening with a long oriented kernel keeps only long
    horizontal/vertical structures, and counting their pixels per row/column
    yields peaks as tall as the line length, regardless of orientation.
    """

    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(2.0, min(gray.shape) / 120))
    detail = np.maximum(
        np.maximum(blur.astype(np.float32) - gray.astype(np.float32), 0),
        np.maximum(gray.astype(np.float32) - blur.astype(np.float32), 0),
    )
    binary = (detail > 12).astype(np.uint8) * 255
    line_len = max(11, int(min(gray.shape) / 55))
    if axis == 0:
        kernel = np.ones((line_len, 1), np.uint8)
        lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
        return lines.mean(axis=0).astype(np.float32)
    kernel = np.ones((1, line_len), np.uint8)
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    return lines.mean(axis=1).astype(np.float32)


def _longest_run(arr: np.ndarray, min_run: int) -> float:
    """Length of the longest contiguous run of ones in a binary row/column,
    or 0.0 when it is shorter than min_run."""
    d = np.diff(np.concatenate(([0], arr, [0])).astype(np.int8))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    if len(starts) == 0:
        return 0.0
    best = int(np.max(ends - starts))
    return float(best) if best >= min_run else 0.0


def _line_run_occupancy_projection(gray: np.ndarray, axis: int) -> np.ndarray:
    """Project long horizontal/vertical line segments by run length.

    Wood-grain boards make the plain occupancy projection unreliable: stone
    edges and the wood/landscape texture respond to the same gradient filter
    as the grid lines, so every row/column inside the board carries an edge
    and the peak train of the true grid is drowned out.  Grid lines are the
    only structures that form continuous runs spanning much of the board, so
    counting only runs longer than a threshold isolates their peaks while
    stone edges (~a stone diameter) and short texture strokes disappear.
    """

    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(2.0, min(gray.shape) / 120))
    detail = np.maximum(
        np.maximum(blur.astype(np.float32) - gray.astype(np.float32), 0),
        np.maximum(gray.astype(np.float32) - blur.astype(np.float32), 0),
    )
    binary = (detail > 12).astype(np.uint8)
    min_run = max(60, int(min(gray.shape) * 0.10))
    out_len = gray.shape[1] if axis == 0 else gray.shape[0]
    proj = np.zeros(out_len, np.float32)
    if axis == 0:  # vertical grid lines -> vertical runs inside each column
        for c in range(gray.shape[1]):
            proj[c] = _longest_run(binary[:, c], min_run)
    else:  # horizontal grid lines -> horizontal runs inside each row
        for r in range(gray.shape[0]):
            proj[r] = _longest_run(binary[r, :], min_run)
    return proj


def _locate_board_by_projection(
    image_rgb: np.ndarray,
    line_count: int,
) -> BoardGeometry:
    """Full-image fallback: locate the grid directly from projection peaks."""

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    for use_runs in (False, True):
        if use_runs:
            x_proj = _line_run_occupancy_projection(gray, 0)
            y_proj = _line_run_occupancy_projection(gray, 1)
        else:
            x_proj = _line_occupancy_projection(gray, 0)
            y_proj = _line_occupancy_projection(gray, 1)
        xs = _find_equidistant_peaks(x_proj, line_count)
        ys = _find_equidistant_peaks(y_proj, line_count)
        if xs is not None and ys is not None and len(xs) == len(ys):
            break
    else:
        raise DetectionError("全图投影中未找到等距网格")
    x_span = float(xs[-1] - xs[0])
    y_span = float(ys[-1] - ys[0])
    if x_span <= 0 or y_span <= 0:
        raise DetectionError("全图投影网格范围异常")
    grid_aspect = min(x_span / y_span, y_span / x_span)
    if grid_aspect < 0.84:
        raise DetectionError("全图投影网格不是近正方形")
    x_spacings = np.diff(xs)
    y_spacings = np.diff(ys)
    regularity = min(
        1.0 - float(np.std(x_spacings) / max(np.mean(x_spacings), 1e-6)) / 0.28,
        1.0 - float(np.std(y_spacings) / max(np.mean(y_spacings), 1e-6)) / 0.28,
    )
    regularity = float(np.clip(regularity, 0.0, 1.0))
    if regularity < 0.42:
        raise DetectionError("全图投影网格规则度不足")
    # Median peak strength of both axes, normalised by the axis' own range so
    # short/long captures share the same scale.
    x_peak_values = x_proj[np.round(xs).astype(int).clip(0, x_proj.shape[0] - 1)]
    y_peak_values = y_proj[np.round(ys).astype(int).clip(0, y_proj.shape[0] - 1)]
    x_range = max(float(np.percentile(x_proj, 97) - np.percentile(x_proj, 55)), 1.0)
    y_range = max(float(np.percentile(y_proj, 97) - np.percentile(y_proj, 55)), 1.0)
    strength = min(
        float(np.median(x_peak_values)) / x_range,
        float(np.median(y_peak_values)) / y_range,
    )
    strength_score = float(np.clip(strength, 0.0, 1.0))
    confidence = float(np.clip(0.75 * strength_score + 0.25 * regularity, 0.0, 1.0))
    return BoardGeometry(xs, ys, confidence, "auto")


def _locate_board_by_frame(
    image_rgb: np.ndarray,
    line_count: int,
) -> BoardGeometry:
    """Locate the grid from the board's extent against its surroundings.

    When stones cover most grid lines (crowded setup screens) neither the
    occupancy projection nor the run-length projection can assemble 15
    equidistant peaks.  The board surface still stands out as a large bright
    rectangle on the dark-wood theme (testnewdar captures) or a dark framed
    board on the light theme; brightness bands of each axis, rather than
    closed contours, are robust to grid lines hidden under stones.  Grid
    lines are placed by subdividing the rectangle, inset by half a cell so
    the outer lines sit on the board rather than on its rim.
    """

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    if min(h, w) < 200:
        raise DetectionError("外框定位：画面过小")
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(2.0, min(gray.shape) / 120))
    t = max(6, int(min(h, w) * 0.08))
    edge = np.concatenate(
        [blur[:t, :].ravel(), blur[-t:, :].ravel(), blur[:, :t].ravel(), blur[:, -t:].ravel()]
    )
    background = float(np.median(edge))
    row_med = np.median(blur, axis=1).astype(np.float32)
    best_rect: tuple[int, int, int, int] | None = None
    best_area = 0.0
    merge_gap = max(15, int(min(h, w) * 0.03))

    def _merge_bands(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not pairs:
            return []
        merged = [list(pairs[0])]
        for a, b in pairs[1:]:
            if a - merged[-1][1] <= merge_gap:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    for sign in (1.0, -1.0):
        m = (row_med - background) * sign > 30.0
        row_bands: list[tuple[int, int]] = []
        i = 0
        while i < h:
            if m[i]:
                j = i
                while j < h and m[j]:
                    j += 1
                row_bands.append((i, j - 1))
                i = j
            else:
                i += 1
        for y0, y1 in _merge_bands(row_bands):
            if y1 - y0 < int(h * 0.25):
                continue
            col_med = np.median(blur[y0 : y1 + 1, :], axis=0).astype(np.float32)
            m2 = (col_med - background) * sign > 30.0
            col_bands: list[tuple[int, int]] = []
            i = 0
            while i < w:
                if m2[i]:
                    j = i
                    while j < w and m2[j]:
                        j += 1
                    col_bands.append((i, j - 1))
                    i = j
                else:
                    i += 1
            for x0, x1 in _merge_bands(col_bands):
                if x1 - x0 < int(w * 0.45):
                    continue
                area = float((y1 - y0 + 1) * (x1 - x0 + 1))
                if area > best_area:
                    best_area = area
                    best_rect = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    if best_rect is None or best_area < (w * h) * 0.12:
        raise DetectionError("外框定位：未找到方形棋盘区域")
    x, y, cw, ch = best_rect
    span_x = float(cw)
    span_y = float(ch)
    grid_aspect = min(span_x / max(span_y, 1e-6), span_y / max(span_x, 1e-6))
    if grid_aspect < 0.85:
        raise DetectionError("外框定位：棋盘区域不是近正方形")
    spacing_x = cw / max(line_count - 1, 1)
    spacing_y = ch / max(line_count - 1, 1)
    xs = np.linspace(x + spacing_x / 2.0, x + cw - spacing_x / 2.0, line_count)
    ys = np.linspace(y + spacing_y / 2.0, y + ch - spacing_y / 2.0, line_count)
    confidence = float(0.72 + 0.18 * grid_aspect)
    return BoardGeometry(xs, ys, confidence, "frame")


def locate_board(image_rgb: np.ndarray, line_count: int = 15) -> BoardGeometry:
    if line_count not in (15, 19):
        raise ValueError("目前仅支持 15 路或 19 路棋盘")
    candidates = _candidate_rectangles(image_rgb)
    if candidates:
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
        if best is not None and best_score >= 0.55:
            # A near-full-width board is located most reliably by its
            # projection peaks; when the colour/contour path produced a much
            # smaller grid the capture likely isolated a decorated sub-region
            # (occasional board-outline mislocation), so prefer the projection.
            try:
                fallback_geometry = _locate_board_by_projection(image_rgb, line_count)
            except DetectionError:
                fallback_geometry = None
            if fallback_geometry is not None:
                fb_w = float(fallback_geometry.x_lines[-1] - fallback_geometry.x_lines[0])
                cand_w = float(best.x_lines[-1] - best.x_lines[0])
                fb_h = float(fallback_geometry.y_lines[-1] - fallback_geometry.y_lines[0])
                cand_h = float(best.y_lines[-1] - best.y_lines[0])
                if fb_w > cand_w * 1.35 and fb_h > cand_h * 1.35:
                    return fallback_geometry
            return best
    # Fallback: boards that fill the capture or share the background hue never
    # produce an isolated rectangle candidate; their grid lines still form a
    # regular peak train in the full-image projection.
    try:
        return _locate_board_by_projection(image_rgb, line_count)
    except DetectionError:
        pass
    # Crowded setup boards hide most grid lines under stones; the dark frame
    # around the board stays visible and gives the grid extent.
    try:
        return _locate_board_by_frame(image_rgb, line_count)
    except DetectionError:
        pass
    if not candidates:
        raise DetectionError("屏幕中没有找到近似方形的棋盘网格")
    raise DetectionError("发现了候选区域，但网格间距不稳定")


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
    outer_radius = max(radius + 2, int(round(geometry.spacing * 0.60)))

    yy, xx = np.ogrid[-outer_radius : outer_radius + 1, -outer_radius : outer_radius + 1]
    distance = np.sqrt(xx * xx + yy * yy)
    inner_mask = distance <= radius
    ring_mask = (distance >= radius * 1.16) & (distance <= outer_radius)

    for row, yf in enumerate(geometry.y_lines):
        for col, xf in enumerate(geometry.x_lines):
            x, y = int(round(float(xf))), int(round(float(yf)))
            x1, x2 = x - outer_radius, x + outer_radius + 1
            y1, y2 = y - outer_radius, y + outer_radius + 1
            patch_gray = crop_cell(gray, x1, y1, x2, y2)
            patch_hsv = crop_cell(hsv, x1, y1, x2, y2)
            sat = patch_hsv[:, :, 1].astype(np.int16)
            val = patch_hsv[:, :, 2].astype(np.int16)

            # Saturated marker pixels (crosses / rings / arrows clients draw on
            # the latest move).  Any hue is treated as a marker: mini-programs
            # use red, green, yellow, blue etc.  The old code only recognized
            # red, so colored markers silently broke the white/black decision.
            # The saturation floor is deliberately high (S>160): wood surfaces
            # commonly sit at S~100 and must NOT be treated as markers.
            bright_marker = (sat > 160) & (val > 160)
            dark_marker = (sat > 160) & (val < 120)
            marker = bright_marker | dark_marker
            clean_gray = patch_gray.astype(np.int16)
            clean_gray[marker] = -1  # excluded from all mean/ratio statistics

            inner = clean_gray[inner_mask].astype(np.float32)
            ring = clean_gray[ring_mask].astype(np.float32)
            inner_kept = inner[inner >= 0]
            ring_kept = ring[ring >= 0]

            inner_pix = patch_gray[inner_mask].astype(np.float32)
            # Black-stone darkness is judged against a threshold that adapts to
            # the local board surface: on a dark-themed board every empty cell is
            # darker than 92, so a fixed threshold would read the whole board as
            # black stones.  The ring median is robust to the grid lines.
            ring_median = float(np.median(patch_gray[ring_mask]))
            # The ring itself may lie inside an oversized stone (diameter close
            # to the cell spacing), pulling ring_median down and with it the
            # darkness threshold; clamp the floor so big dark stones still read.
            dark_threshold = min(92.0, max(ring_median - 15.0, 75.0))
            dark_ratio = float(np.mean(inner_pix < dark_threshold))
            bright_neutral = (patch_gray > 215) & (sat < 45)
            white_ratio = float(np.mean(bright_neutral[inner_mask]))
            marker_ratio = float(np.mean(marker[inner_mask]))

            if inner_kept.size < max(8, int(inner_mask.sum() * 0.45)):
                # A marker covers most of the cell; fall back to the raw pixels
                # so a dense cross does not turn a stone into "empty".
                inner_kept = inner_pix
                ring_kept = ring.astype(np.float32) if ring_kept.size == 0 else ring_kept
                if inner_kept.size == 0:
                    inner_kept = inner_pix

            inner_mean = float(np.mean(inner_kept))
            ring_mean = float(np.mean(ring_kept)) if ring_kept.size else float(np.mean(ring))

            black_strength = max((ring_mean - inner_mean - 25) / 75, (dark_ratio - 0.26) / 0.58)
            # 五子棋达人 black stones are not solid black: a bright gradient
            # rim sits around a dark core, so the fraction of pixels below the
            # darkness threshold hovers around 0.5 and occasional renders drop
            # below the 0.43 bar.  A dark inner median on a bright ring is a
            # strong independent black signal that does not fire on empty
            # cells (bright) or on dark-themed boards (ring not bright).
            # The thresholds are relaxed from (110, 135, 0.12) to (120, 125,
            # 0.26): resizing the client window re-rasterizes the stone and can
            # brighten the core / darken the rim slightly.  Measured on
            # errtu.png a missed black stone read im=91, rm=129, white=0.233
            # while every empty cell sits at im>=220, so the wider band still
            # separates cleanly.
            inner_median = float(np.median(inner_pix))
            if inner_median < 120.0 and ring_median > 125.0 and white_ratio < 0.26:
                black_strength = max(black_strength, 0.58)
            # The centre-to-ring contrast term assumes a bright neutral stone on
            # a darker surface.  The new wood-grain board is itself yellowish and
            # saturated (S~90), so its darker landscape motifs pull ring_mean down
            # and would read as "white stone"; gate the contrast term on a low
            # inner saturation, which real white stones always satisfy (S~0).
            inner_sat_median = float(np.median(sat[inner_mask])) if inner_mask.sum() else 255.0
            white_strength = max((white_ratio - 0.36) / 0.58, 0.0)
            if inner_sat_median < 60.0:
                white_strength = max(white_strength, (inner_mean - ring_mean - 18) / 62)
            if inner_mean - ring_mean >= 20 and white_ratio >= 0.45 and dark_ratio < 0.12:
                plain_white = 0.58 + min(0.38, (inner_mean - ring_mean - 20) / 55)
                white_strength = max(white_strength, plain_white)
            # Bright stone with ANY marker (colored cross, or a dark cross that
            # lowers center-to-ring contrast): the bright-neutral inner fraction
            # is the robust signal.  Dark grid lines on empty cells keep their
            # dark_ratio high, so this does not fire on empty intersections.
            if white_ratio >= 0.45 and dark_ratio < 0.20:
                plain_white = 0.55 + min(0.40, (white_ratio - 0.45) * 1.2)
                white_strength = max(white_strength, plain_white)
            # Colored marker on top of an otherwise neutral center: combine the
            # marker evidence with a clean center-to-ring contrast instead of
            # requiring the specific red + white combination.  The dark frame
            # around the wood-grain board also saturates, so the clean center
            # must be genuinely bright (a real stone is, the frame is not).
            if (
                marker_ratio >= 0.06
                and inner_mean >= 215.0
                and inner_mean - ring_mean >= 5
                and dark_ratio < 0.14
                and inner_sat_median < 60.0
            ):
                marked_white = 0.54 + min(0.40, (marker_ratio - 0.06) * 2.5)
                white_strength = max(white_strength, marked_white)
            # Acceptance bar relaxed from 0.43 to 0.40: resizing the client
            # window re-rasterizes stones and the weakest real black stone
            # measured on errtu.png drops to black_strength=0.425, while every
            # empty cell stays at bs<=-0.15 and every white stone at bs<=-0.36,
            # so 0.40 still leaves a wide safety margin against false blacks.
            if black_strength >= 0.40 and black_strength > white_strength + 0.08:
                board[row, col] = BLACK
                confidence[row, col] = float(np.clip(black_strength, 0, 1))
            elif white_strength >= 0.43 and white_strength > black_strength + 0.08:
                board[row, col] = WHITE
                confidence[row, col] = float(np.clip(white_strength, 0, 1))
            else:
                confidence[row, col] = float(np.clip(1 - max(black_strength, white_strength), 0, 1))
    return board, confidence


def find_confirm_button(
    image_rgb: np.ndarray,
    geometry: BoardGeometry,
    *,
    min_area: int = 400,
) -> tuple[int, int] | None:
    """Locate the yellow confirm button below the board (image coordinates).

    Mini-program clients such as 五子棋达人 commit a move by tapping a yellow
    confirm button under the grid after placing the stone.  The new build adds
    a yellow "cancel" button directly above the confirm one, so the search is
    restricted to a strip 1.5-3.5 grid spacings below the board (cancel sits at
    ~1.0 spacing and is excluded) and horizontally centred on the board, and
    the click point is the centroid of the yellow pixels inside the strip:
    the confirm button dominates the strip, while corner badges and side
    buttons fall outside it.
    """

    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    # Yellow band relaxed from (s>=110, v>=150) to (s>=95, v>=140): measured on
    # the light-background build (newconfirm.png) and the old 20okbv.png the
    # matched confirm-button pixels bottom out at s=110, v=167, so resizing the
    # client window re-rasterizes the button and can drop its saturation below
    # the old bar.  In the search strip the extra pixels the relaxed band admits
    # (30 on the light build, 96 on 20okbv) all lie inside the button itself,
    # so the centroid does not move.
    yellow = (h >= 12) & (h <= 48) & (s >= 95) & (v >= 140)
    left, _, right, bottom = geometry.bounds
    spacing = geometry.spacing
    center_x = (left + right) / 2.0
    search_y0 = int(round(bottom + spacing * 1.75))
    search_y1 = int(round(bottom + spacing * 3.5)) + 1
    x0 = int(round(center_x - spacing * 1.8))
    x1 = int(round(center_x + spacing * 1.8)) + 1
    if search_y1 - search_y0 < 8 or search_y0 >= image_rgb.shape[0] - 8:
        return None
    x0 = max(0, x0)
    x1 = min(image_rgb.shape[1], x1)
    if x1 - x0 < 8:
        return None
    roi = yellow[search_y0:search_y1, x0:x1]
    if int(roi.sum()) < min_area:
        return None
    ys, xs = np.nonzero(roi)
    return (int(round(x0 + float(xs.mean()))), int(round(search_y0 + float(ys.mean()))))
