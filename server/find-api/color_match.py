"""Color stage for Find drone: HSV/Lab masks, tiled blobs, fused with shape.

No neural nets. Designed to stay reasonable on large mosaics by working in
tiles and using a downscaled preview only to estimate the field background.
"""

from __future__ import annotations

import math
from typing import Iterable

import cv2
import numpy as np

from matcher import Match, _iou, _nms

# HSV ranges are OpenCV-scale (H 0-180, S/V 0-255).
PRESETS: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {
    "red": [((0, 70, 60), (10, 255, 255)), ((170, 70, 60), (180, 255, 255))],
    "orange": [((8, 80, 70), (25, 255, 255))],
    "yellow": [((22, 60, 80), (38, 255, 255))],
    "white": [((0, 0, 190), (180, 50, 255))],
    "gray": [((0, 0, 70), (180, 50, 190))],
    "blue": [((95, 50, 40), (130, 255, 255))],
}

# Typical small aircraft on a survey mosaic, meters across.
DRONE_MIN_M = 0.25
DRONE_MAX_M = 2.8

# Small-marking search: a near-ground frame (0.2–1 m AGL) has a tiny nadir
# GSD, so a 6 cm target is "expected" at hundreds of pixels and car windows
# pass the size gate. Floor GSD and skip very low altitudes.
GSD_FLOOR_SMALL_M = 0.002  # 0.2 cm/px; 6 cm → 30 px expected
MIN_ALTITUDE_SMALL_M = 3.0
# Unfloored expected size (px) above this implies the camera is within a couple of metres.
CLOSE_RANGE_EXPECTED_PX = 40
MAX_ASPECT_SMALL = 4.0
SHAPE_MIN_SCORE_SMALL = 0.60
# Glass / smooth panels: only applied to blobs large enough for a stable gradient.
GLASS_MIN_SIDE = 12
GLASS_MIN_AREA = 80


def to_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def extract_dominant_colors(image: np.ndarray, k: int = 4) -> list[tuple[int, int, int]]:
    """Return up to k dominant BGR colors from a reference photo, ignoring near-black borders."""
    bgr = to_bgr(image)
    small = bgr
    h, w = bgr.shape[:2]
    if max(h, w) > 256:
        scale = 256 / max(h, w)
        small = cv2.resize(bgr, (max(8, int(w * scale)), max(8, int(h * scale))), interpolation=cv2.INTER_AREA)
    pixels = small.reshape(-1, 3).astype(np.float32)
    # Drop near-black / near-white margin noise so a drone on a photo stand still works.
    lum = pixels.mean(axis=1)
    keep = (lum > 18) & (lum < 245)
    if int(keep.sum()) < 32:
        keep = np.ones(len(pixels), dtype=bool)
    pixels = pixels[keep]
    k_use = max(1, min(k, len(pixels) // 8, 6))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _compact, labels, centers = cv2.kmeans(pixels, k_use, None, criteria, 4, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k_use)
    order = np.argsort(counts)[::-1]
    colors = []
    for i in order:
        if counts[i] < max(8, 0.04 * labels.size):
            continue
        b, g, r = (int(round(v)) for v in centers[i])
        colors.append((b, g, r))
    return colors or [(int(centers[0][0]), int(centers[0][1]), int(centers[0][2]))]


def _hsv_band(h, dh, lo_s, lo_v, hi_s=255, hi_v=255):
    if h < dh or h > 180 - dh:
        return [
            ((0, lo_s, lo_v), (min(180, h + dh if h < 90 else dh), hi_s, hi_v)),
            ((max(0, h - dh if h > 90 else 180 - dh), lo_s, lo_v), (180, hi_s, hi_v)),
        ]
    return [((max(0, h - dh), lo_s, lo_v), (min(180, h + dh), hi_s, hi_v))]


def bgr_to_hsv_range_sets(
    bgr: tuple[int, int, int],
) -> tuple[
    list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    list[tuple[tuple[int, int, int], tuple[int, int, int]]],
]:
    """Return (operational tight / band 1, extra-loose fallback / band 2).

    Band 1 is the widened primary used in 0.5.3 as the first loose=True band:
    wide enough for JPEG-blended paint, but still unique to the target color
    (on the Avata frames it hits only the fin pixels). Band 2 is the much
    duller field-wide band and must only be used as a fallback.
    """
    swatch = np.uint8([[list(bgr)]])
    h, s, v = (int(x) for x in cv2.cvtColor(swatch, cv2.COLOR_BGR2HSV)[0, 0])
    if s < 40 and v > 185:
        return list(PRESETS["white"]), []
    if s < 40:
        return list(PRESETS["gray"]), []
    dh, ds, dv = (18, 90, 90)
    lo_s = min(max(18, s - ds), max(18, int(s * 0.35)))
    lo_v = min(max(28, v - dv), max(28, int(v * 0.40)))
    primary = _hsv_band(h, dh, lo_s, lo_v)
    d_s, d_v = max(12, int(s * 0.12)), max(28, int(v * 0.32))
    hi_s = max(d_s + 24, int(s * 0.75))
    extra = _hsv_band(h, 20, d_s, d_v, hi_s=hi_s, hi_v=min(255, v + 20))
    return primary, extra


def bgr_to_hsv_ranges(
    bgr: tuple[int, int, int],
    *,
    loose: bool = False,
) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    if not loose:
        swatch = np.uint8([[list(bgr)]])
        h, s, v = (int(x) for x in cv2.cvtColor(swatch, cv2.COLOR_BGR2HSV)[0, 0])
        if s < 40 and v > 185:
            return PRESETS["white"]
        if s < 40:
            return PRESETS["gray"]
        dh, ds, dv = (12, 70, 70)
        lo_s, lo_v = max(30, s - ds), max(40, v - dv)
        return _hsv_band(h, dh, lo_s, lo_v)
    primary, extra = bgr_to_hsv_range_sets(bgr)
    return primary + extra


def ranges_for_request(
    *,
    preset: str | None = None,
    hex_color: str | None = None,
    reference: np.ndarray | None = None,
    loose: bool = False,
) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    if hex_color:
        text = hex_color.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
            return bgr_to_hsv_ranges((b, g, r), loose=loose)
    if preset and preset.lower() in PRESETS:
        if loose and preset.lower() not in {"white", "gray"}:
            # Widen the preset the same way a custom swatch is widened.
            mid = PRESETS[preset.lower()][0]
            # Rebuild from mid-hue of the first band.
            lo, hi = mid
            mid_h = (int(lo[0]) + int(hi[0])) // 2
            mid_s = max(int(lo[1]), 80)
            mid_v = max(int(lo[2]), 80)
            return bgr_to_hsv_ranges(_hsv_mid_bgr(mid_h, mid_s, mid_v), loose=True)
        return list(PRESETS[preset.lower()])
    if reference is not None and reference.size:
        ranges: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
        for color in extract_dominant_colors(reference):
            ranges.extend(bgr_to_hsv_ranges(color, loose=loose))
        return ranges
    return []


def range_sets_for_request(
    *,
    preset: str | None = None,
    hex_color: str | None = None,
    reference: np.ndarray | None = None,
) -> tuple[
    list[tuple[tuple[int, int, int], tuple[int, int, int]]],
    list[tuple[tuple[int, int, int], tuple[int, int, int]]],
]:
    """Return (tight / band 1, loose-fallback / band 2). Loose is only used if tight finds nothing."""
    if hex_color:
        bgr = hex_to_bgr(hex_color)
        if bgr is not None:
            return bgr_to_hsv_range_sets(bgr)
    if preset and preset.lower() in PRESETS:
        tight = list(PRESETS[preset.lower()])
        if preset.lower() in {"white", "gray"}:
            return tight, []
        lo, hi = tight[0]
        mid_h = (int(lo[0]) + int(hi[0])) // 2
        mid_s = max(int(lo[1]), 80)
        mid_v = max(int(lo[2]), 80)
        _primary, extra = bgr_to_hsv_range_sets(_hsv_mid_bgr(mid_h, mid_s, mid_v))
        tight_set = set(tight)
        return tight, [b for b in extra if b not in tight_set]
    if reference is not None and reference.size:
        tight: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
        loose: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = []
        for color in extract_dominant_colors(reference):
            t, l = bgr_to_hsv_range_sets(color)
            tight.extend(t)
            loose.extend(l)
        tight_set = set(tight)
        return tight, [b for b in loose if b not in tight_set]
    return [], []


def _hsv_mid_bgr(h: int, s: int, v: int) -> tuple[int, int, int]:
    sw = np.uint8([[[h, s, v]]])
    b, g, r = (int(x) for x in cv2.cvtColor(sw, cv2.COLOR_HSV2BGR)[0, 0])
    return (b, g, r)


def hex_to_bgr(hex_color: str | None) -> tuple[int, int, int] | None:
    if not hex_color:
        return None
    text = hex_color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return (b, g, r)


def expected_target_px(
    gsd_m: float | None,
    target_size_m: float | None,
    target_size_px: float | None = None,
    *,
    size_mode: str | None = None,
    floor_gsd: bool = True,
) -> float | None:
    if target_size_px and target_size_px > 0:
        return float(target_size_px)
    gsd = gsd_for_size_gate(gsd_m, size_mode) if floor_gsd else (float(gsd_m) if gsd_m and gsd_m > 0 else None)
    if gsd and target_size_m and gsd > 0 and target_size_m > 0:
        return float(target_size_m) / float(gsd)
    return None


def gsd_for_size_gate(gsd_m: float | None, size_mode: str | None = None) -> float | None:
    if not gsd_m or gsd_m <= 0:
        return None
    gsd = float(gsd_m)
    if is_small_marking(size_mode):
        return max(gsd, GSD_FLOOR_SMALL_M)
    return gsd


def should_skip_low_altitude(
    *,
    size_mode: str | None,
    altitude_m: float | None = None,
    gsd_m: float | None = None,
    target_size_m: float | None = None,
    min_altitude_m: float | None = None,
) -> str | None:
    """Return a skip reason for near-ground small-marking frames, or None."""
    if not is_small_marking(size_mode):
        return None
    # min_altitude_m <= 0 disables the skip (explicit override).
    if min_altitude_m is not None and min_altitude_m <= 0:
        return None
    min_alt = MIN_ALTITUDE_SMALL_M if min_altitude_m is None else float(min_altitude_m)
    if altitude_m is not None and altitude_m < min_alt:
        return "low_altitude"
    if gsd_m and gsd_m > 0 and target_size_m and target_size_m > 0:
        expected = float(target_size_m) / float(gsd_m)  # unfloored
        if expected > CLOSE_RANGE_EXPECTED_PX:
            return "low_altitude"
    return None


def is_small_marking(size_mode: str | None) -> bool:
    return (size_mode or "").strip().lower() in {"small_marking", "marking", "fin"}


def _mask_hsv(bgr: np.ndarray, ranges) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in ranges:
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8)))
    return mask


def _background_mask(bgr: np.ndarray) -> np.ndarray:
    """A loose mask of typical field greens and browns, used to drop background blobs."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (30, 25, 25), (95, 255, 220))
    brown = cv2.inRange(hsv, (5, 25, 25), (30, 200, 200))
    return cv2.bitwise_or(green, brown)


def _blob_size_ok(
    w: float,
    h: float,
    gsd_m: float | None,
    query_wh: tuple[int, int] | None,
    *,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
) -> bool:
    area = w * h
    span = max(w, h)
    expected = expected_target_px(gsd_m, target_size_m, target_size_px, size_mode=size_mode)
    if expected:
        # Thin markings (e.g. an 8x3 px fin): judge by the long side, allow a short min side.
        return (0.3 * expected) <= span <= (3.5 * expected) and min(w, h) >= 2 and area >= 3
    small = is_small_marking(size_mode)
    if small:
        if gsd_m and gsd_m > 0:
            span_m = span * gsd_m
            # 2 cm – 80 cm markings, not the 25 cm aircraft floor.
            return 0.02 <= span_m <= 0.80 and min(w, h) >= 2 and area >= 3
        return min(w, h) >= 2 and span <= 400 and area >= 3
    if query_wh:
        qw, qh = query_wh
        qarea = max(64, qw * qh)
        closeup = max(qw, qh) >= 120 and area < 0.25 * qarea
        if closeup and gsd_m is None:
            # A close-up reference is much larger than the on-mosaic marking;
            # do not reject solely because the blob is < 0.25 x the query.
            pass
        elif area < 0.25 * qarea or area > 8.0 * qarea:
            if gsd_m is None:
                return False
        else:
            if gsd_m is None:
                return True
    if gsd_m and gsd_m > 0:
        span_m = span * gsd_m
        return DRONE_MIN_M <= span_m <= DRONE_MAX_M * 1.6
    return 8 <= min(w, h) and span <= 400 and area >= 64


def gsd_meters(georef) -> float | None:
    if georef is None:
        return None
    pw = abs(float(getattr(georef, "pixel_w", 0) or 0))
    ph = abs(float(getattr(georef, "pixel_h", 0) or 0))
    if pw <= 0 and ph <= 0:
        return None
    px = pw or ph
    crs = str(getattr(georef, "crs", "") or "")
    if crs in {"EPSG:4326", "geographic", "bounds"} or crs.endswith(":4326"):
        # Degrees → meters (approx at the top-left latitude).
        _x, lat = georef.pixel_to_xy(0, 0)
        import math

        return px * 111_320.0 * max(0.2, math.cos(math.radians(lat)))
    if px < 1:  # still might be degrees with an unknown CRS
        if px < 0.01:
            return px * 111_320.0
    return px


def _lab(bgr: tuple[int, int, int] | np.ndarray) -> np.ndarray:
    arr = np.uint8([[list(bgr)]]) if not isinstance(bgr, np.ndarray) or bgr.ndim == 1 else bgr
    if arr.ndim == 1:
        arr = np.uint8([[arr.tolist()]])
    return cv2.cvtColor(arr, cv2.COLOR_BGR2LAB).astype(np.float32)


def _blob_color_score(
    tile_bgr: np.ndarray,
    mask: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    fill: float,
    target_bgr: tuple[int, int, int] | None,
) -> float:
    """Rank by color distance to the picked/reference paint and contrast vs the local background."""
    H, W = tile_bgr.shape[:2]
    x1, y1 = min(W, x + w), min(H, y + h)
    patch = tile_bgr[y:y1, x:x1]
    m = mask[y:y1, x:x1]
    if patch.size == 0:
        return 0.0
    if int(np.count_nonzero(m)) >= 2:
        mean_bgr = tuple(int(v) for v in cv2.mean(patch, mask=m)[:3])
    else:
        mean_bgr = tuple(int(v) for v in patch.reshape(-1, 3).mean(axis=0))
    closeness = 0.5
    if target_bgr is not None:
        d = float(np.linalg.norm(_lab(mean_bgr) - _lab(target_bgr)))
        # CIE Lab: ~20 is a close match, ~80+ is a different color.
        closeness = float(np.clip(1.0 - d / 90.0, 0.0, 1.0))
    # Local background ring around the blob.
    pad = max(4, int(max(w, h)))
    rx0, ry0 = max(0, x - pad), max(0, y - pad)
    rx1, ry1 = min(W, x1 + pad), min(H, y1 + pad)
    ring = tile_bgr[ry0:ry1, rx0:rx1].copy()
    ly, lx = y - ry0, x - rx0
    ring[ly : ly + (y1 - y), lx : lx + (x1 - x)] = 0
    ring_px = ring.reshape(-1, 3)
    ring_px = ring_px[np.any(ring_px != 0, axis=1)]
    contrast = 0.4
    if len(ring_px) >= 8:
        bg = ring_px.mean(axis=0)
        contrast = float(np.clip(np.linalg.norm(np.array(mean_bgr, np.float32) - bg) / 80.0, 0.0, 1.0))
    return float(min(0.97, 0.15 + 0.50 * closeness + 0.25 * contrast + 0.10 * fill))


TIGHT_BAND_BONUS = 0.15
ISOLATED_COLOR_BOOST = 0.18
# If a loose HSV band covers more than this fraction of the frame, treat it as
# field texture rather than a paint signal (the Avata stubble case is ~4.6%).
LOOSE_COVERAGE_START = 0.002
LOOSE_COVERAGE_ZERO = 0.008
LOOSE_MIN_SCORE = 0.55


def _collect_color_hits(
    bgr: np.ndarray,
    ranges,
    *,
    top_n: int,
    gsd_m,
    query_wh,
    tile: int,
    overlap: int,
    size_mode: str,
    target_size_m,
    target_size_px,
    target_bgr,
    small: bool,
    field_frac: float,
) -> list[Match]:
    if not ranges:
        return []
    H, W = bgr.shape[:2]
    hits: list[Match] = []
    step = max(256, tile - overlap)
    for y0 in range(0, H, step):
        for x0 in range(0, W, step):
            x1 = min(W, x0 + tile)
            y1 = min(H, y0 + tile)
            tile_bgr = np.ascontiguousarray(bgr[y0:y1, x0:x1])
            mask = _mask_hsv(tile_bgr, ranges)
            if field_frac > 0.15 and not small:
                mask = cv2.bitwise_and(mask, cv2.bitwise_not(_background_mask(tile_bgr)))
            if small:
                # A 3x3 open deletes an 8x3 / few-pixel marking.
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            else:
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            min_area = 3 if small else 24
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                if not _blob_size_ok(
                    w,
                    h,
                    gsd_m,
                    query_wh,
                    size_mode=size_mode,
                    target_size_m=target_size_m,
                    target_size_px=target_size_px,
                ):
                    continue
                area = cv2.contourArea(cnt)
                if area < min_area:
                    continue
                fill = area / max(1.0, w * h)
                score = _blob_color_score(tile_bgr, mask, x, y, w, h, fill, target_bgr)
                hits.append(
                    Match(
                        x=float(x0 + x),
                        y=float(y0 + y),
                        w=float(w),
                        h=float(h),
                        score=score,
                        method="color",
                    )
                )
    return hits


def _looks_like_glass(bgr: np.ndarray, x: float, y: float, w: float, h: float) -> bool:
    """Specular / low-saturation-gradient panels (car glass, paint). Tiny fins are skipped."""
    if min(w, h) < GLASS_MIN_SIDE and w * h < GLASS_MIN_AREA:
        return False
    H, W = bgr.shape[:2]
    x0 = max(0, int(x)); y0 = max(0, int(y))
    x1 = min(W, int(math.ceil(x + w))); y1 = min(H, int(math.ceil(y + h)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return False
    crop = bgr[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1].astype(np.float32)
    v = hsv[..., 2].astype(np.float32)
    gs = cv2.Sobel(s, cv2.CV_32F, 1, 1, ksize=3)
    sgrad = float(np.mean(np.abs(gs)))
    # Smooth bluish glass: low S gradient, modest S std, often a bright highlight.
    highlight = float(np.mean(v > 200))
    if sgrad <= 2.4 and float(s.std()) < 28:
        return True
    if sgrad <= 3.0 and highlight >= 0.08 and float(s.std()) < 40:
        return True
    return False


def _reject_structured_false_positive(
    bgr: np.ndarray,
    m: Match,
    *,
    size_mode: str,
    horizon=None,
) -> bool:
    """Reject poles, glass, huge uniform panels, and above-horizon blobs in small-marking mode."""
    if not is_small_marking(size_mode):
        return False
    w, h = float(m.w), float(m.h)
    if min(w, h) < 1:
        return True
    if max(w, h) / min(w, h) >= MAX_ASPECT_SMALL:
        return True
    if horizon is not None and horizon.excludes_box(m.x, m.y, w, h):
        return True
    if _looks_like_glass(bgr, m.x, m.y, w, h):
        return True
    return False


def _loose_rarity(global_frac: float) -> float:
    if global_frac <= LOOSE_COVERAGE_START:
        return 1.0
    if global_frac >= LOOSE_COVERAGE_ZERO:
        return 0.0
    return float((LOOSE_COVERAGE_ZERO - global_frac) / (LOOSE_COVERAGE_ZERO - LOOSE_COVERAGE_START))


def match_color(
    mosaic_bgr: np.ndarray,
    *,
    ranges,
    top_n: int = 8,
    gsd_m: float | None = None,
    query_wh: tuple[int, int] | None = None,
    tile: int = 1024,
    overlap: int = 64,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
    target_bgr: tuple[int, int, int] | None = None,
    loose_ranges=None,
    gimbal_pitch_deg: float | None = None,
    focal_px: float | None = None,
    horizon=None,
) -> list[Match]:
    """Find color-anomalous blobs vs a green/brown field, size-filtered by GSD or marking size.

    Tight (band-1) ranges are preferred and scored higher. The optional loose
    band is only used when the tight band finds nothing, and is suppressed
    when it covers a large fraction of the frame (field-wide noise).
    """
    if mosaic_bgr is None or mosaic_bgr.size == 0:
        return []
    if not ranges and not loose_ranges:
        return []
    bgr = to_bgr(mosaic_bgr)
    H, W = bgr.shape[:2]
    small = is_small_marking(size_mode) or (expected_target_px(gsd_m, target_size_m, target_size_px) or 99) < 16
    preview_max = 1024
    scale = min(1.0, preview_max / max(H, W))
    preview = cv2.resize(bgr, (max(8, int(W * scale)), max(8, int(H * scale))), interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    field = _background_mask(preview)
    field_frac = float(np.count_nonzero(field)) / field.size

    kw = dict(
        top_n=top_n,
        gsd_m=gsd_m,
        query_wh=query_wh,
        tile=tile,
        overlap=overlap,
        size_mode=size_mode,
        target_size_m=target_size_m,
        target_size_px=target_size_px,
        target_bgr=target_bgr,
        small=small,
        field_frac=field_frac,
    )
    if horizon is None and is_small_marking(size_mode):
        try:
            from horizon import detect_horizon
            horizon = detect_horizon(bgr, gimbal_pitch_deg=gimbal_pitch_deg, focal_px=focal_px)
        except Exception:
            horizon = None

    tight = _collect_color_hits(bgr, ranges, **kw) if ranges else []
    if tight:
        for m in tight:
            m.score = float(min(0.99, m.score + TIGHT_BAND_BONUS))
        tight = [m for m in tight if not _reject_structured_false_positive(bgr, m, size_mode=size_mode, horizon=horizon)]
        return _nms(tight, 0.3)[: max(top_n * 3, 16)]

    if not loose_ranges:
        return []
    loose_mask = _mask_hsv(bgr, loose_ranges)
    global_frac = float(np.count_nonzero(loose_mask)) / loose_mask.size
    rarity = _loose_rarity(global_frac)
    if rarity <= 0:
        return []
    loose_hits = _collect_color_hits(bgr, loose_ranges, **kw)
    kept: list[Match] = []
    for m in loose_hits:
        # Penalize blobs sitting in a neighborhood that is also mostly loose-band.
        x0 = max(0, int(m.x) - int(max(8, m.w)))
        y0 = max(0, int(m.y) - int(max(8, m.h)))
        x1 = min(W, int(m.x + m.w) + int(max(8, m.w)))
        y1 = min(H, int(m.y + m.h) + int(max(8, m.h)))
        neigh = loose_mask[y0:y1, x0:x1]
        local_frac = float(np.count_nonzero(neigh)) / neigh.size if neigh.size else 0.0
        isolation = 1.0 if local_frac <= global_frac * 1.5 and local_frac < 0.15 else max(0.0, 1.0 - local_frac)
        score = float(m.score) * rarity * (0.5 + 0.5 * isolation)
        if score < LOOSE_MIN_SCORE:
            continue
        m.score = score
        if _reject_structured_false_positive(bgr, m, size_mode=size_mode, horizon=horizon):
            continue
        kept.append(m)
    return _nms(kept, 0.3)[: max(top_n * 3, 16)]


def fuse_matches(
    shape_hits: Iterable[Match],
    color_hits: Iterable[Match],
    *,
    top_n: int = 8,
    iou_thresh: float = 0.2,
    size_mode: str = "aircraft",
) -> list[Match]:
    """Merge shape and color into one ranked list with method color, shape, or color+shape."""
    shapes = list(shape_hits)
    colors = list(color_hits)
    used_color = set()
    fused: list[Match] = []
    for s in shapes:
        best_i, best_iou = -1, 0.0
        for i, c in enumerate(colors):
            if i in used_color:
                continue
            v = _iou(s, c)
            if v > best_iou:
                best_iou, best_i = v, i
        if best_i >= 0 and best_iou >= iou_thresh:
            c = colors[best_i]
            used_color.add(best_i)
            # Union box, blended score.
            x0 = min(s.x, c.x)
            y0 = min(s.y, c.y)
            x1 = max(s.x + s.w, c.x + c.w)
            y1 = max(s.y + s.h, c.y + c.h)
            fused.append(
                Match(
                    x=x0,
                    y=y0,
                    w=x1 - x0,
                    h=y1 - y0,
                    score=float(min(0.99, 0.55 * s.score + 0.45 * c.score + 0.08)),
                    method="color+shape",
                    angle=s.angle,
                    scale=s.scale,
                )
            )
        else:
            s.method = s.method or "shape"
            if s.method not in {"shape", "color", "color+shape"}:
                # Template/feature methods stay identifiable but count as shape for the API contract.
                s.method = "shape"
            fused.append(s)
    for i, c in enumerate(colors):
        if i not in used_color:
            # Isolated tight-band color hits should outrank shape-only stubble.
            c.score = float(min(0.99, c.score + ISOLATED_COLOR_BOOST))
            fused.append(c)
    merged = _nms(fused, 0.3)
    if is_small_marking(size_mode):
        merged = [m for m in merged if m.method != "shape" or m.score >= SHAPE_MIN_SCORE_SMALL]
    merged.sort(key=lambda m: m.score, reverse=True)
    # Keep a few color / color+shape slots so Match color ON still surfaces them.
    colorish = [m for m in merged if m.method in {"color", "color+shape"}]
    reserve = colorish[: min(3, top_n, len(colorish))]
    remaining = [m for m in merged if m not in reserve]
    slots = max(0, top_n - len(reserve))
    out = remaining[:slots] + reserve
    out.sort(key=lambda m: m.score, reverse=True)
    return out[:top_n]


def search_with_color(
    mosaic_gray: np.ndarray,
    query_gray: np.ndarray,
    mosaic_bgr: np.ndarray | None,
    query_bgr: np.ndarray | None,
    *,
    mode: str = "color+shape",
    top_n: int = 8,
    scales=None,
    angles=None,
    threshold: float = 0.45,
    preset: str | None = None,
    hex_color: str | None = None,
    gsd_m: float | None = None,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
    timeout_s: float | None = None,
    altitude_m: float | None = None,
    min_altitude_m: float | None = None,
    gimbal_pitch_deg: float | None = None,
    focal_px: float | None = None,
    horizon=None,
) -> list[Match]:
    from matcher import search as shape_search

    mode = (mode or "color+shape").strip().lower()
    if mode in {"shape+color", "both", "all"}:
        mode = "color+shape"
    if mode not in {"color", "shape", "color+shape"}:
        mode = "color+shape"

    if should_skip_low_altitude(
        size_mode=size_mode,
        altitude_m=altitude_m,
        gsd_m=gsd_m,
        target_size_m=target_size_m,
        min_altitude_m=min_altitude_m,
    ):
        return []

    shape_hits: list[Match] = []
    if mode in {"shape", "color+shape"} and mosaic_gray is not None and query_gray is not None:
        shape_hits = shape_search(
            mosaic_gray,
            query_gray,
            top_n=top_n,
            scales=scales,
            angles=angles,
            threshold=threshold,
            timeout_s=timeout_s,
            size_mode=size_mode,
            target_size_m=target_size_m,
            target_size_px=target_size_px,
            gsd_m=gsd_m,
        )
        for m in shape_hits:
            if m.method not in {"color", "color+shape"}:
                m.method = "shape"

    color_hits: list[Match] = []
    if mode in {"color", "color+shape"} and mosaic_bgr is not None:
        q = query_bgr if query_bgr is not None else query_gray
        use_loose = is_small_marking(size_mode) or (expected_target_px(gsd_m, target_size_m, target_size_px) or 99) < 16
        tight, loose_bands = range_sets_for_request(preset=preset, hex_color=hex_color, reference=q)
        if not use_loose:
            loose_bands = []
        qh, qw = (query_gray.shape[0], query_gray.shape[1]) if query_gray is not None else (None, None)
        target_bgr = hex_to_bgr(hex_color)
        if target_bgr is None and q is not None and getattr(q, "ndim", 0) == 3:
            cols = extract_dominant_colors(q, k=2)
            target_bgr = cols[0] if cols else None
        color_hits = match_color(
            mosaic_bgr,
            ranges=tight,
            loose_ranges=loose_bands,
            top_n=top_n,
            gsd_m=gsd_m,
            query_wh=(qw, qh) if qw else None,
            size_mode=size_mode,
            target_size_m=target_size_m,
            target_size_px=target_size_px,
            target_bgr=target_bgr,
            gimbal_pitch_deg=gimbal_pitch_deg,
            focal_px=focal_px,
            horizon=horizon,
        )

    if mode == "shape":
        return shape_hits[:top_n]
    if mode == "color":
        return _nms(color_hits, 0.3)[:top_n]
    return fuse_matches(shape_hits, color_hits, top_n=top_n, size_mode=size_mode)
