from pathlib import Path
import sys
import math

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from horizon import detect_horizon, horizon_row_from_pitch, HorizonInfo
from color_match import (
    should_skip_low_altitude,
    gsd_for_size_gate,
    expected_target_px,
    GSD_FLOOR_SMALL_M,
    match_color,
    range_sets_for_request,
    search_with_color,
    fuse_matches,
)
from matcher import Match


def test_horizon_row_from_pitch_sign():
    # Looking down 10°: horizon is above the image centre.
    row = horizon_row_from_pitch(-10.0, 2160, 2095.31)
    assert row is not None
    assert row < 1080
    # Looking up 10°: horizon is below the centre.
    row_up = horizon_row_from_pitch(10.0, 2160, 2095.31)
    assert row_up > 1080


def _sky_field(h=480, w=640, horizon=160):
    img = np.zeros((h, w, 3), np.uint8)
    # Bright, low-texture sky
    img[:horizon] = (210, 200, 190)
    # Textured green-brown field
    rng = np.random.default_rng(0)
    field = rng.integers(40, 90, size=(h - horizon, w, 3), dtype=np.uint8)
    field[..., 1] = np.clip(field[..., 1] + 40, 0, 255)
    img[horizon:] = field
    return img


def test_detect_horizon_on_synthetic_sky():
    img = _sky_field()
    info = detect_horizon(img)
    assert info.horizon_row is not None
    assert abs(info.horizon_row - 160) <= 25
    assert info.source in {"sky", "sky+pitch"}
    # A box in the sky is excluded; a box in the field is not.
    assert info.excludes_box(100, 40, 20, 10) is True
    assert info.excludes_box(100, 360, 8, 6) is False


def test_skip_low_altitude_and_tiny_gsd():
    assert should_skip_low_altitude(size_mode="small_marking", altitude_m=0.8, target_size_m=0.06) == "low_altitude"
    assert should_skip_low_altitude(size_mode="small_marking", altitude_m=13.0, gsd_m=0.01016, target_size_m=0.06) is None
    # Unfloored expected size at 0.2 m-scale GSD is huge.
    assert should_skip_low_altitude(size_mode="small_marking", gsd_m=0.0002, target_size_m=0.06) == "low_altitude"
    assert should_skip_low_altitude(size_mode="small_marking", gsd_m=0.0002, target_size_m=0.06, min_altitude_m=0) is None
    assert should_skip_low_altitude(size_mode="aircraft", altitude_m=0.8) is None
    assert gsd_for_size_gate(0.0002, "small_marking") == GSD_FLOOR_SMALL_M
    # After the floor, a 6 cm target is ~30 px, not 300.
    assert abs(expected_target_px(0.0002, 0.06, size_mode="small_marking") - (0.06 / GSD_FLOOR_SMALL_M)) < 1e-9


def test_gsd_floor_rejects_huge_window_keeps_fin():
    img = _sky_field(h=400, w=600, horizon=80)
    # Tight-band blue paint
    from color_match import hex_to_bgr
    bgr = hex_to_bgr("#1955AA")
    # Tiny fin in the field
    img[300:306, 200:207] = bgr
    # Huge car-window-sized panel also in the field
    img[200:300, 350:550] = bgr
    tight, loose = range_sets_for_request(hex_color="#1955AA")
    hits = match_color(
        img, ranges=tight, loose_ranges=[], top_n=16,
        size_mode="small_marking", target_size_m=0.06, gsd_m=0.0002,
    )
    # The 200x100 panel must not pass the floored size gate (max ~105 px).
    assert all(max(m.w, m.h) < 120 for m in hits)
    assert any(abs(m.x - 200) <= 4 and abs(m.y - 300) <= 4 for m in hits)


def test_rejects_pole_and_sky_blob():
    img = _sky_field(h=400, w=600, horizon=120)
    from color_match import hex_to_bgr
    bgr = hex_to_bgr("#1955AA")
    img[20:70, 300:308] = bgr  # 8x50 pole in the sky
    img[300:308, 200:208] = bgr  # 8x8 fin in the field
    tight, _ = range_sets_for_request(hex_color="#1955AA")
    hits = match_color(img, ranges=tight, loose_ranges=[], top_n=16, size_mode="small_marking")
    assert any(abs(m.x - 200) <= 4 and abs(m.y - 300) <= 4 for m in hits)
    assert not any(m.y < 120 and max(m.w, m.h) >= 20 for m in hits)


def test_search_skips_low_altitude_frames():
    img = _sky_field()
    from color_match import hex_to_bgr
    img[300:308, 200:208] = hex_to_bgr("#1955AA")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    q = img[300:308, 200:208]
    hits = search_with_color(
        gray, cv2.cvtColor(q, cv2.COLOR_BGR2GRAY), img, q,
        mode="color", top_n=8, hex_color="#1955AA", size_mode="small_marking",
        target_size_m=0.06, gsd_m=0.0002, altitude_m=0.5,
    )
    assert hits == []


def test_fuse_drops_low_shape_in_small_marking():
    color = [Match(x=10, y=10, w=8, h=8, score=0.80, method="color")]
    shape = [Match(x=200, y=200, w=20, h=16, score=0.52, method="shape")]
    out = fuse_matches(shape, color, top_n=8, size_mode="small_marking")
    assert out and out[0].method == "color"
    assert all(m.method != "shape" or m.score >= 0.60 for m in out)
    kept = fuse_matches(
        [Match(x=200, y=200, w=20, h=16, score=0.72, method="shape")],
        color, top_n=8, size_mode="small_marking",
    )
    assert any(m.method == "shape" for m in kept)


def test_detects_partial_width_sky():
    """Sky that covers only the left ~20% of the top of the frame must still be masked."""
    h, w, horizon = 480, 640, 150
    img = np.zeros((h, w, 3), np.uint8)
    rng = np.random.default_rng(1)
    field = rng.integers(40, 90, size=(h, w, 3), dtype=np.uint8)
    field[..., 1] = np.clip(field[..., 1] + 40, 0, 255)
    img[:] = field
    img[:horizon, : int(0.22 * w)] = (210, 200, 190)
    info = detect_horizon(img)
    assert info.horizon_row is not None
    assert abs(info.horizon_row - horizon) <= 30
    assert info.excludes_box(20, 20, 16, 10) is True
    assert info.excludes_box(400, 360, 8, 6) is False
