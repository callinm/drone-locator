"""
Tiled pyramid matcher for a large orthophoto / mosaic.

Small or distant targets: multi-scale, multi-rotation normalized
cross-correlation (OpenCV TM_CCOEFF_NORMED) with non-maximum suppression,
refined from a coarse pyramid into local full-resolution tiles.

Larger textured targets: ORB (and AKAZE if the OpenCV build has it, else
SIFT) with a Lowe ratio test and RANSAC homography.

Results are merged, NMS'd, and ranked. No neural nets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import time

import cv2
import numpy as np

from config import CV_THREADS, MAX_WORKING_DIM, MATCH_TIMEOUT_S

cv2.setNumThreads(CV_THREADS)

DEFAULT_SCALES = [0.6, 0.8, 1.0, 1.25, 1.5]
DEFAULT_ANGLES = [-30.0, -15.0, 0.0, 15.0, 30.0]
PEAKS_PER_POSE = 96
MAX_PRE_NMS = 400
NMS_IOU = 0.3


class MatchBudget:
    """Cooperative per-request time budget. When expired, return best-so-far."""

    def __init__(self, seconds: float | None):
        if seconds is None:
            seconds = MATCH_TIMEOUT_S
        self.seconds = float(seconds) if seconds and seconds > 0 else 0.0
        self.deadline = (time.monotonic() + self.seconds) if self.seconds else None
        self.timed_out = False

    def expired(self) -> bool:
        if self.deadline is None:
            return False
        if time.monotonic() >= self.deadline:
            self.timed_out = True
            return True
        return False


def scales_for_query(
    query: np.ndarray,
    *,
    target_size_m: float | None = None,
    gsd_m: float | None = None,
    size_mode: str = "aircraft",
    target_size_px: float | None = None,
) -> list[float]:
    """Default 0.6–1.5x, plus finer scales when the query is a close-up of a small marking."""
    scales = list(DEFAULT_SCALES)
    qmin = float(min(query.shape[:2]))
    qmax = float(max(query.shape[:2]))
    expected_px = None
    if target_size_px and target_size_px > 0:
        expected_px = float(target_size_px)
    elif target_size_m and gsd_m and target_size_m > 0 and gsd_m > 0 and qmax >= 4:
        expected_px = float(target_size_m) / float(gsd_m)
    extra: list[float] = []
    if expected_px and qmax >= 8:
        # Scale against the long side of a close-up so an 8 px fin in a 213 px photo is tried.
        min_scale = max(8.0 / qmax, (expected_px / qmax) * 0.5)
        s = min_scale
        while s < 0.6 - 1e-6:
            extra.append(round(s, 4))
            s *= 1.55
    elif (size_mode or "").lower() in {"small_marking", "marking", "fin"} and qmax >= 32:
        min_scale = max(8.0 / qmax, 0.04)
        s = min_scale
        while s < 0.6 - 1e-6:
            extra.append(round(s, 4))
            s *= 1.55
    out: list[float] = []
    for s in extra + scales:
        if s <= 0:
            continue
        if not out or abs(s - out[-1]) > 0.02:
            out.append(float(s))
    return out


@dataclass
class Match:
    x: float
    y: float
    w: float
    h: float
    score: float
    method: str
    lat: float | None = None
    lon: float | None = None
    angle: float = 0.0
    scale: float = 1.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # API schema uses width/height names? The brief says {x,y,w,h,score,method,lat,lon}
        return {
            "x": d["x"],
            "y": d["y"],
            "w": d["w"],
            "h": d["h"],
            "score": d["score"],
            "method": d["method"],
            "lat": d["lat"],
            "lon": d["lon"],
            "angle": d["angle"],
            "scale": d["scale"],
        }


def _rotate_bound(image: np.ndarray, angle_deg: float) -> tuple[np.ndarray, tuple[int, int]]:
    """Rotate around center, expanding the canvas. Returns (rotated, (cx, cy))."""
    if abs(angle_deg) < 1e-6:
        h, w = image.shape[:2]
        return image, (w / 2.0, h / 2.0)
    h, w = image.shape[:2]
    c = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(c, angle_deg, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw = int(h * sin + w * cos)
    nh = int(h * cos + w * sin)
    M[0, 2] += nw / 2.0 - c[0]
    M[1, 2] += nh / 2.0 - c[1]
    rotated = cv2.warpAffine(
        image, M, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0
    )
    return rotated, (nw / 2.0, nh / 2.0)


def _resize(image: np.ndarray, scale: float) -> np.ndarray:
    if abs(scale - 1.0) < 1e-6:
        return image
    h, w = image.shape[:2]
    nw = max(8, int(round(w * scale)))
    nh = max(8, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(image, (nw, nh), interpolation=interp)


def _nms(matches: list[Match], iou_thresh: float = NMS_IOU, max_keep: int | None = None) -> list[Match]:
    """Greedy NMS. Uses OpenCV's vectorized boxes when the list is long."""
    if not matches:
        return []
    if len(matches) == 1:
        return list(matches)
    # Cap first so a dense 4K frame cannot quadratic-explode.
    ordered = sorted(matches, key=lambda m: m.score, reverse=True)
    if len(ordered) > MAX_PRE_NMS:
        ordered = ordered[:MAX_PRE_NMS]
    if len(ordered) <= 64:
        kept: list[Match] = []
        for m in ordered:
            if all(_iou(m, k) < iou_thresh for k in kept):
                kept.append(m)
                if max_keep and len(kept) >= max_keep:
                    break
        return kept
    boxes = [[m.x, m.y, m.w, m.h] for m in ordered]
    scores = [float(m.score) for m in ordered]
    try:
        idxs = cv2.dnn.NMSBoxes(boxes, scores, score_threshold=0.0, nms_threshold=float(iou_thresh))
    except Exception:
        idxs = None
    if idxs is None or len(idxs) == 0:
        kept = []
        for m in ordered:
            if all(_iou(m, k) < iou_thresh for k in kept):
                kept.append(m)
                if max_keep and len(kept) >= max_keep:
                    break
        return kept
    kept_i = [int(i) for i in np.array(idxs).reshape(-1)]
    kept_i.sort(key=lambda i: ordered[i].score, reverse=True)
    if max_keep:
        kept_i = kept_i[:max_keep]
    return [ordered[i] for i in kept_i]


def _iou(a: Match, b: Match) -> float:
    x0 = max(a.x, b.x)
    y0 = max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w)
    y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _tiled_ncc(
    image: np.ndarray,
    templ: np.ndarray,
    threshold: float,
    tile: int = 1024,
    max_peaks: int = PEAKS_PER_POSE,
    budget: MatchBudget | None = None,
) -> list[tuple[float, int, int]]:
    """Return (score, x, y) peaks of templ inside image, in image coordinates."""
    th, tw = templ.shape[:2]
    h, w = image.shape[:2]
    if th >= h or tw >= w or th < 4 or tw < 4:
        return []
    step = max(1, tile - max(th, tw) + 1)
    if step > tile:
        step = tile
    peaks: list[tuple[float, int, int]] = []
    kernel = np.ones((3, 3), np.uint8)
    for y0 in range(0, h - th + 1, step):
        y1 = min(h, y0 + tile)
        if y1 - y0 < th:
            continue
        if budget and budget.expired():
            break
        for x0 in range(0, w - tw + 1, step):
            x1 = min(w, x0 + tile)
            if x1 - x0 < tw:
                continue
            patch = image[y0:y1, x0:x1]
            try:
                res = cv2.matchTemplate(patch, templ, cv2.TM_CCOEFF_NORMED)
            except cv2.error:
                continue
            # Vectorized 3x3 local maxima above threshold.
            above = res >= threshold
            if not np.any(above):
                continue
            dilated = cv2.dilate(res, kernel)
            ys, xs = np.where(above & (res >= dilated - 1e-9))
            if ys.size == 0:
                continue
            vals = res[ys, xs]
            if vals.size > max_peaks:
                pick = np.argpartition(vals, -max_peaks)[-max_peaks:]
                ys, xs, vals = ys[pick], xs[pick], vals[pick]
            for score, xx, yy in zip(vals.tolist(), xs.tolist(), ys.tolist()):
                peaks.append((float(score), x0 + int(xx), y0 + int(yy)))
    if len(peaks) > max_peaks:
        peaks.sort(key=lambda p: p[0], reverse=True)
        peaks = peaks[:max_peaks]
    return peaks


def _pyramid(mosaic: np.ndarray, query: np.ndarray | None = None) -> list[tuple[float, np.ndarray]]:
    """Coarser-to-finer list of (scale_relative_to_original, image).

    The finest *dense* level is capped at MAX_WORKING_DIM. Full-resolution
    is not copied here; callers refine in tiles against the original.
    The coarsest scale is also bounded so a typical query stays at least
    ~16 px — an 8 px template at 1/8 scale is too ambiguous.
    """
    h, w = mosaic.shape[:2]
    max_dim = max(h, w)
    levels: list[tuple[float, np.ndarray]] = []
    qmin = min(query.shape[:2]) if query is not None else 32
    min_scale_for_query = 16.0 / max(qmin, 16)
    # Prefer a global search around 2–4k wide, but not coarser than min_scale_for_query.
    target_coarse = min(1.0, 4096 / max_dim)
    start = max(min_scale_for_query, target_coarse, 1.0 / 16)
    if max_dim * start > MAX_WORKING_DIM:
        start = MAX_WORKING_DIM / max_dim
    scale = start
    while scale <= 1.0 + 1e-9 and max_dim * scale <= MAX_WORKING_DIM + 1:
        img = mosaic if scale >= 0.999 else _resize(mosaic, scale)
        levels.append((1.0 if scale >= 0.999 else scale, img))
        if scale >= 0.999 or max_dim * scale * 2 > MAX_WORKING_DIM + 1:
            break
        scale = min(1.0, scale * 2)
    if not levels:
        s = min(1.0, MAX_WORKING_DIM / max_dim)
        levels.append((s, mosaic if s >= 0.999 else _resize(mosaic, s)))
    return levels


def match_template_pyramid(
    mosaic: np.ndarray,
    query: np.ndarray,
    *,
    scales: list[float] | None = None,
    angles: list[float] | None = None,
    threshold: float = 0.45,
    top_n: int = 8,
    timeout_s: float | None = None,
    budget: MatchBudget | None = None,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
    gsd_m: float | None = None,
) -> list[Match]:
    if scales is None:
        scales = scales_for_query(
            query,
            target_size_m=target_size_m,
            gsd_m=gsd_m,
            size_mode=size_mode,
            target_size_px=target_size_px,
        )
    if angles is None:
        angles = list(DEFAULT_ANGLES)
    budget = budget or MatchBudget(timeout_s)
    levels = _pyramid(mosaic, query)
    # Use the coarsest level for a global search.
    coarse_scale, coarse_img = levels[0]
    hits: list[Match] = []
    for qscale in scales:
        if budget.expired():
            break
        q0 = _resize(query, qscale)
        for angle in angles:
            if budget.expired():
                break
            rot, _ = _rotate_bound(q0, angle)
            if rot.shape[0] >= coarse_img.shape[0] or rot.shape[1] >= coarse_img.shape[1]:
                continue
            templ = _resize(rot, coarse_scale) if coarse_scale < 0.999 else rot
            if templ.shape[0] < 8 or templ.shape[1] < 8:
                continue
            if templ.shape[0] >= coarse_img.shape[0] or templ.shape[1] >= coarse_img.shape[1]:
                continue
            pose_thr = max(0.35, threshold - 0.1)
            # Sub-16 px templates match 4K stubble far too easily.
            if min(templ.shape[:2]) < 16 or qscale < 0.4:
                pose_thr = max(pose_thr, 0.62)
            peaks = _tiled_ncc(
                coarse_img,
                templ,
                threshold=pose_thr,
                max_peaks=PEAKS_PER_POSE,
                budget=budget,
            )
            for score, px, py in peaks:
                # Map to original pixels. The template size in original space
                # is rot's size (query at qscale, rotated).
                x = px / coarse_scale
                y = py / coarse_scale
                adj = float(score)
                side = min(rot.shape[:2])
                if side < 16 or qscale < 0.4:
                    adj *= max(0.5, min(1.0, side / 16.0))
                hits.append(
                    Match(
                        x=x,
                        y=y,
                        w=float(rot.shape[1]),
                        h=float(rot.shape[0]),
                        score=adj,
                        method="template",
                        angle=float(angle),
                        scale=float(qscale),
                    )
                )
    # Score-cap then NMS at a sane IoU so a dense 32x32 query cannot hang.
    hits = _nms(hits, NMS_IOU)
    hits.sort(key=lambda m: m.score, reverse=True)
    refined: list[Match] = []
    fine_scale, fine_img = levels[-1]
    for m in hits[: max(top_n * 8, 32)]:
        if budget.expired():
            # Keep the coarse score if we run out of time mid-refine.
            refined.append(m)
            continue
        refined.append(_refine_hit(mosaic, fine_img, fine_scale, query, m, threshold))
    # If we timed out before refining, still return the best coarse peaks.
    min_score = threshold if not budget.timed_out else min(threshold, 0.35)
    refined = [m for m in refined if m.score >= min_score]
    return _nms(refined, NMS_IOU)[:top_n]


def _refine_hit(
    mosaic: np.ndarray,
    fine_img: np.ndarray,
    fine_scale: float,
    query: np.ndarray,
    hit: Match,
    threshold: float,
) -> Match:
    q = _resize(query, hit.scale)
    rot, _ = _rotate_bound(q, hit.angle)
    # Search a padded window around the coarse hit in the finest dense level,
    # then (if mosaic is larger than fine_img) in a native tile.
    pad = max(rot.shape) + 16
    cx = hit.x + hit.w / 2
    cy = hit.y + hit.h / 2
    # Fine-level window
    fx0 = max(0, int(cx * fine_scale - pad * fine_scale))
    fy0 = max(0, int(cy * fine_scale - pad * fine_scale))
    fx1 = min(fine_img.shape[1], int(cx * fine_scale + pad * fine_scale))
    fy1 = min(fine_img.shape[0], int(cy * fine_scale + pad * fine_scale))
    fine_t = rot if abs(fine_scale - 1) < 1e-6 else _resize(rot, fine_scale)
    window = fine_img[fy0:fy1, fx0:fx1]
    best = hit
    if window.shape[0] > fine_t.shape[0] and window.shape[1] > fine_t.shape[1]:
        try:
            res = cv2.matchTemplate(window, fine_t, cv2.TM_CCOEFF_NORMED)
            minv, maxv, minl, maxl = cv2.minMaxLoc(res)
            if maxv >= hit.score * 0.9:
                x = (fx0 + maxl[0]) / fine_scale
                y = (fy0 + maxl[1]) / fine_scale
                best = Match(
                    x=x,
                    y=y,
                    w=float(rot.shape[1]),
                    h=float(rot.shape[0]),
                    score=float(maxv),
                    method="template",
                    angle=hit.angle,
                    scale=hit.scale,
                )
        except cv2.error:
            pass
    # Native tile refine if we still have a coarser fine_img than the original.
    if fine_scale < 0.999:
        h, w = mosaic.shape[:2]
        x0 = max(0, int(best.x) - pad)
        y0 = max(0, int(best.y) - pad)
        x1 = min(w, int(best.x + best.w) + pad)
        y1 = min(h, int(best.y + best.h) + pad)
        tile = mosaic[y0:y1, x0:x1]
        if tile.shape[0] > rot.shape[0] and tile.shape[1] > rot.shape[1]:
            try:
                res = cv2.matchTemplate(tile, rot, cv2.TM_CCOEFF_NORMED)
                _minv, maxv, _minl, maxl = cv2.minMaxLoc(res)
                if maxv >= best.score * 0.9:
                    best = Match(
                        x=float(x0 + maxl[0]),
                        y=float(y0 + maxl[1]),
                        w=float(rot.shape[1]),
                        h=float(rot.shape[0]),
                        score=float(maxv),
                        method="template",
                        angle=hit.angle,
                        scale=hit.scale,
                    )
            except cv2.error:
                pass
    return best


def _make_detector(kind: str):
    if kind == "AKAZE" and hasattr(cv2, "AKAZE_create"):
        return cv2.AKAZE_create(), cv2.NORM_HAMMING, "akaze"
    if kind == "SIFT" and hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=2000), cv2.NORM_L2, "sift"
    return cv2.ORB_create(nfeatures=2500), cv2.NORM_HAMMING, "orb"


def match_features(
    mosaic: np.ndarray,
    query: np.ndarray,
    *,
    ratio: float = 0.75,
    min_inliers: int = 8,
) -> list[Match]:
    """ORB (plus AKAZE or SIFT) on a working-resolution mosaic."""
    if min(query.shape[:2]) < 40:
        return []
    h, w = mosaic.shape[:2]
    feature_dim = min(MAX_WORKING_DIM, 3000)
    if max(h, w) > feature_dim:
        scale = feature_dim / max(h, w)
        work = _resize(mosaic, scale)
    else:
        scale = 1.0
        work = mosaic

    # AKAZE is not in every OpenCV wheel (the 5.x headless build we use has
    # ORB + SIFT). SIFT's scale-space is memory-heavy, so only use it on
    # modest working images.
    kinds = ["ORB"]
    if hasattr(cv2, "AKAZE_create"):
        kinds.append("AKAZE")
    elif hasattr(cv2, "SIFT_create") and max(work.shape) <= 2048:
        kinds.append("SIFT")

    results: list[Match] = []
    for kind in kinds:
        det, norm, name = _make_detector(kind)
        try:
            kp_q, d_q = det.detectAndCompute(query, None)
            kp_m, d_m = det.detectAndCompute(work, None)
        except cv2.error:
            continue
        if d_q is None or d_m is None or len(kp_q) < 8 or len(kp_m) < 8:
            continue
        matcher = cv2.BFMatcher(norm, crossCheck=False)
        try:
            raw = matcher.knnMatch(d_q, d_m, k=2)
        except cv2.error:
            continue
        good = []
        for pair in raw:
            if len(pair) < 2:
                continue
            a, b = pair
            if a.distance < ratio * b.distance:
                good.append(a)
        if len(good) < min_inliers:
            continue
        src = np.float32([kp_q[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp_m[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if H is None or mask is None:
            continue
        inliers = int(mask.ravel().sum())
        if inliers < min_inliers:
            continue
        qh, qw = query.shape[:2]
        corners = np.float32([[0, 0], [qw, 0], [qw, qh], [0, qh]]).reshape(-1, 1, 2)
        proj = cv2.perspectiveTransform(corners, H).reshape(-1, 2)
        proj /= scale
        xs, ys = proj[:, 0], proj[:, 1]
        x, y = float(xs.min()), float(ys.min())
        ww, hh = float(xs.max() - x), float(ys.max() - y)
        if ww < 4 or hh < 4:
            continue
        inlier_ratio = inliers / max(len(good), 1)
        score = float(min(0.99, 0.35 + 0.5 * inlier_ratio + 0.015 * min(inliers, 25)))
        results.append(Match(x=x, y=y, w=ww, h=hh, score=score, method=name))
    return results


def search(
    mosaic: np.ndarray,
    query: np.ndarray,
    *,
    top_n: int = 8,
    scales: list[float] | None = None,
    angles: list[float] | None = None,
    threshold: float = 0.45,
    timeout_s: float | None = None,
    size_mode: str = "aircraft",
    target_size_m: float | None = None,
    target_size_px: float | None = None,
    gsd_m: float | None = None,
    budget: MatchBudget | None = None,
) -> list[Match]:
    """Run template + feature matching and return the top-N merged hits."""
    budget = budget or MatchBudget(timeout_s)
    if scales is None:
        scales = scales_for_query(
            query,
            target_size_m=target_size_m,
            gsd_m=gsd_m,
            size_mode=size_mode,
            target_size_px=target_size_px,
        )
    templ = match_template_pyramid(
        mosaic,
        query,
        scales=scales,
        angles=angles,
        threshold=threshold,
        top_n=top_n,
        budget=budget,
        size_mode=size_mode,
        target_size_m=target_size_m,
        target_size_px=target_size_px,
        gsd_m=gsd_m,
    )
    feats: list[Match] = []
    if not budget.expired():
        feats = match_features(mosaic, query)
    merged = _nms(templ + feats, NMS_IOU)
    if (size_mode or "").lower() in {"small_marking", "marking", "fin"}:
        # A close-up of a painted marking has little unique texture; NCC
        # on field stubble otherwise outranks a correct isolated color hit.
        qmax = max(query.shape[:2])
        factor = 0.65 if qmax >= 64 else 0.85
        for m in merged:
            m.score = float(m.score) * factor
    return merged[:top_n]


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY if image.shape[2] == 3 else cv2.COLOR_BGR2GRAY)
