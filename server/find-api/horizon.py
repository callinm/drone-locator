"""Sky / horizon masking for Find drone.

Visual detection of a bright, low-texture sky region (typical on tilted
survey frames). When a DJI gimbal pitch and a focal length in pixels are
available they are used as a fallback or a consistency check — never as the
only mask on a frame that has no visible sky, so a field-filling downward
look is not cropped by a telemetry guess.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np


# Exclude a band just below the horizon: nadir GSD under-sizes distant objects.
FAR_FIELD_FRAC = 0.08
FAR_FIELD_MIN_PX = 40


@dataclass
class HorizonInfo:
    horizon_row: int | None
    sky_mask: np.ndarray | None
    source: str | None
    far_margin_px: int = 0
    gimbal_pitch_deg: float | None = None

    def cutoff_row(self) -> int | None:
        if self.horizon_row is None:
            return None
        return int(self.horizon_row + self.far_margin_px)

    def excludes_box(self, x: float, y: float, w: float, h: float) -> bool:
        """True if the blob is at/above the horizon or mostly in the sky mask."""
        cy = y + h / 2.0
        cut = self.cutoff_row()
        if cut is not None and cy <= cut:
            return True
        if self.sky_mask is None or self.sky_mask.size == 0:
            return False
        H, W = self.sky_mask.shape[:2]
        x0 = max(0, int(math.floor(x)))
        y0 = max(0, int(math.floor(y)))
        x1 = min(W, int(math.ceil(x + w)))
        y1 = min(H, int(math.ceil(y + h)))
        if x1 <= x0 or y1 <= y0:
            return False
        patch = self.sky_mask[y0:y1, x0:x1]
        if patch.size and float(np.count_nonzero(patch)) / patch.size >= 0.45:
            return True
        # Neighborhood also mostly sky → pole-against-dusk, etc.
        pad = int(max(12, max(w, h)))
        nx0 = max(0, x0 - pad)
        ny0 = max(0, y0 - pad)
        nx1 = min(W, x1 + pad)
        ny1 = min(H, y1 + pad)
        neigh = self.sky_mask[ny0:ny1, nx0:nx1]
        if neigh.size and float(np.count_nonzero(neigh)) / neigh.size >= 0.55:
            return True
        return False


def horizon_row_from_pitch(
    pitch_deg: float,
    image_height_px: int,
    focal_px: float,
) -> int | None:
    """Row of the geometric horizon.

    DJI gimbal pitch: 0 looks at the horizon, negative looks down (nadir is -90).
    """
    if image_height_px <= 0 or not focal_px or focal_px <= 0:
        return None
    try:
        row = int(round(image_height_px / 2.0 + float(focal_px) * math.tan(math.radians(float(pitch_deg)))))
    except (TypeError, ValueError, OverflowError):
        return None
    return row


def detect_horizon(
    bgr: np.ndarray,
    *,
    gimbal_pitch_deg: float | None = None,
    focal_px: float | None = None,
) -> HorizonInfo:
    """Detect the horizon and a sky mask on a BGR frame."""
    if bgr is None or bgr.size == 0:
        return HorizonInfo(None, None, None)
    if bgr.ndim == 2:
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    H, W = bgr.shape[:2]
    far = max(FAR_FIELD_MIN_PX, int(FAR_FIELD_FRAC * H))
    pitch = None
    try:
        if gimbal_pitch_deg is not None:
            pitch = float(gimbal_pitch_deg)
    except (TypeError, ValueError):
        pitch = None
    hz_pitch = horizon_row_from_pitch(pitch, H, focal_px) if (pitch is not None and focal_px) else None

    scale = 0.5 if max(H, W) >= 1280 else 1.0
    im = (
        cv2.resize(bgr, (max(8, int(W * scale)), max(8, int(H * scale))), interpolation=cv2.INTER_AREA)
        if scale != 1.0
        else bgr
    )
    h, w = im.shape[:2]
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    lum = cv2.cvtColor(im, cv2.COLOR_BGR2LAB)[..., 0].astype(np.int16)
    lap = cv2.blur(np.abs(cv2.Laplacian(gray, cv2.CV_32F, ksize=3)), (15, 15))
    # Bright and low-texture — dusk skies are only moderately bright.
    cand = ((lap < 7) & (lum > 88)).astype(np.uint8)
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(cand, 4)
    sky = np.zeros((h, w), np.uint8)
    min_top = max(400, int(0.003 * h * w))
    min_large = max(2000, int(0.015 * h * w))
    top_lim = max(2, int(0.03 * h))
    for i in range(1, n_lbl):
        area = int(stats[i, cv2.CC_STAT_AREA])
        y0 = int(stats[i, cv2.CC_STAT_TOP])
        width_i = int(stats[i, cv2.CC_STAT_WIDTH])
        # A sky patch often covers only part of the width (trees, a wing, vignetting).
        touches_top = y0 <= top_lim
        wide_top = touches_top and width_i >= 0.12 * w and area >= min_top
        top_chunk = touches_top and area >= max(min_top, int(0.01 * h * w))
        large_upper = y0 < 0.40 * h and area >= min_large
        if wide_top or top_chunk or large_upper:
            sky[labels == i] = 255

    hz_vis = None
    if sky.any():
        cols = sky.any(axis=0)
        # Lowest sky pixel in each column that has sky.
        low = np.where(cols, h - 1 - np.argmax(sky[::-1], axis=0), -1)
        vals = low[cols]
        if vals.size:
            hz_vis = int(np.median(vals))

    source = None
    hz_full = None
    sky_full = None
    if hz_vis is not None:
        hz_full = int(round(hz_vis / scale))
        source = "sky"
        sky_full = cv2.resize(sky, (W, H), interpolation=cv2.INTER_NEAREST)
        # If gimbal pitch agrees reasonably, record a combined source.
        if hz_pitch is not None and 0 <= hz_pitch < H and abs(hz_full - hz_pitch) <= 0.2 * H:
            source = "sky+pitch"
    elif hz_pitch is not None and 0 <= hz_pitch < H and pitch is not None and abs(pitch) <= 35:
        # Only trust pitch when there is at least some sky-like signal in the upper third.
        upper = cand[: max(4, int(0.33 * h))]
        if upper.size and float(np.count_nonzero(upper)) / upper.size >= 0.08:
            hz_full = int(hz_pitch)
            source = "pitch"
            # Approximate a sky mask above the geometric horizon.
            sky_full = np.zeros((H, W), np.uint8)
            cut = max(0, min(H, hz_full + far // 4))
            sky_full[:cut] = 255

    return HorizonInfo(
        horizon_row=hz_full,
        sky_mask=sky_full,
        source=source,
        far_margin_px=far,
        gimbal_pitch_deg=pitch,
    )
