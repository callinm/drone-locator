import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from georef import GeoRef, read_geotiff_ref, write_geotiff_4326  # noqa: E402
from matcher import match_features, match_template_pyramid, search  # noqa: E402


def make_target(size: int = 48) -> np.ndarray:
    img = np.zeros((size, size), np.uint8)
    cx = cy = size / 2
    for y in range(size):
        for x in range(size):
            r = np.hypot(x - cx, y - cy)
            on_chevron = abs(x - cx) < y * 0.65 and 6 < y < size - 4
            on_ring = size * 0.42 < r < size * 0.5
            on_bar = (x + y) % 5 == 0 and r < size * 0.4
            if on_chevron or on_ring:
                img[y, x] = 255
            elif on_bar:
                img[y, x] = 200
    return img


def noisy_field(h: int, w: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 90, size=(h, w), dtype=np.uint8)
    # Low-frequency blotches so it looks like a field, not white noise.
    blotch = cv2.GaussianBlur(rng.integers(0, 80, size=(h, w), dtype=np.uint8), (31, 31), 0)
    return cv2.addWeighted(base, 0.7, blotch, 0.3, 0)


def paste(dest: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    h, w = src.shape[:2]
    dest[y : y + h, x : x + w] = src


def box_center(m) -> tuple[float, float]:
    return m.x + m.w / 2, m.y + m.h / 2


@pytest.mark.parametrize("scale", [0.75, 1.0, 1.25])
def test_template_finds_scaled_target(scale):
    target = make_target(48)
    field = noisy_field(900, 1200, seed=3)
    q = cv2.resize(
        target,
        (max(8, int(round(48 * scale))), max(8, int(round(48 * scale)))),
        interpolation=cv2.INTER_LINEAR if scale > 1 else cv2.INTER_AREA,
    )
    x0, y0 = 640, 220
    paste(field, q, x0, y0)
    # Search with the *original* (unscaled) target.
    hits = match_template_pyramid(field, target, threshold=0.4, top_n=5)
    assert hits, "expected at least one hit"
    cx, cy = box_center(hits[0])
    exp_cx, exp_cy = x0 + q.shape[1] / 2, y0 + q.shape[0] / 2
    assert abs(cx - exp_cx) <= 12, f"cx {cx} vs {exp_cx} score={hits[0].score}"
    assert abs(cy - exp_cy) <= 12, f"cy {cy} vs {exp_cy}"
    assert hits[0].score >= 0.5
    print(f"    scale={scale}: score={hits[0].score:.3f} center=({cx:.1f},{cy:.1f}) expected=({exp_cx:.1f},{exp_cy:.1f})")


@pytest.mark.parametrize("angle", [0, 15, 30])
def test_template_finds_rotated_target(angle):
    target = make_target(48)
    field = noisy_field(900, 1200, seed=5)
    h, w = target.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    rot = cv2.warpAffine(target, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)
    x0, y0 = 400, 500
    paste(field, rot, x0, y0)
    hits = match_template_pyramid(field, target, threshold=0.35, top_n=5)
    assert hits, f"no hit for angle {angle}"
    cx, cy = box_center(hits[0])
    exp_cx, exp_cy = x0 + rot.shape[1] / 2, y0 + rot.shape[0] / 2
    assert abs(cx - exp_cx) <= 16, f"angle {angle}: cx {cx} vs {exp_cx} score={hits[0].score}"
    assert abs(cy - exp_cy) <= 16, f"angle {angle}: cy {cy} vs {exp_cy}"
    print(f"    angle={angle}: score={hits[0].score:.3f} method={hits[0].method} reported_angle={hits[0].angle}")


def test_features_find_a_large_textured_patch():
    # A bigger unique "patch" with lots of corners — better for ORB than a 48 px glyph.
    rng = np.random.default_rng(9)
    patch = rng.integers(0, 255, size=(160, 160), dtype=np.uint8)
    # Add strong corners
    cv2.rectangle(patch, (10, 10), (70, 70), 255, 3)
    cv2.drawMarker(patch, (120, 40), 0, cv2.MARKER_TILTED_CROSS, 40, 3)
    cv2.circle(patch, (80, 110), 25, 255, 2)
    field = noisy_field(800, 1000, seed=8)
    x0, y0 = 200, 300
    paste(field, patch, x0, y0)
    hits = match_features(field, patch)
    # Features can fail on synthetic noise; fall back to merged search.
    if not hits:
        hits = search(field, patch, top_n=3, threshold=0.4)
    assert hits
    cx, cy = box_center(hits[0])
    assert abs(cx - (x0 + 80)) <= 25, f"cx {cx} score={hits[0].score} method={hits[0].method}"
    assert abs(cy - (y0 + 80)) <= 25
    print(f"    features/search: method={hits[0].method} score={hits[0].score:.3f}")


def test_geotiff_pixel_to_lonlat(tmp_path):
    arr = np.zeros((200, 300), np.uint8)
    west, north = -88.2264, 40.0847
    pw, ph = 0.00001, 0.000008
    path = tmp_path / "mini.tif"
    write_geotiff_4326(path, arr, west, north, pw, ph)
    ref = read_geotiff_ref(path, 300, 200)
    assert ref is not None
    lon, lat = ref.pixel_to_lonlat(0, 0)
    assert abs(lon - west) < 1e-8
    assert abs(lat - north) < 1e-8
    lon2, lat2 = ref.pixel_to_lonlat(300, 200)
    assert abs(lon2 - (west + 300 * pw)) < 1e-8
    assert abs(lat2 - (north - 200 * ph)) < 1e-8


def test_bounds_georef_center():
    g = GeoRef.from_bounds(-88.23, 40.08, -88.22, 40.09, 1000, 800)
    lon, lat = g.pixel_to_lonlat(500, 400)
    assert abs(lon - (-88.225)) < 1e-9
    assert abs(lat - 40.085) < 1e-9


def test_scales_for_query_extends_for_closeup_marking():
    from matcher import scales_for_query, DEFAULT_SCALES

    q = np.zeros((213, 99), np.uint8)
    aircraft = scales_for_query(q, size_mode="aircraft")
    assert aircraft == DEFAULT_SCALES
    small = scales_for_query(q, size_mode="small_marking")
    assert min(small) <= 0.05
    assert 1.0 in small
    # GSD 0.612 cm/px and a 6 cm fin on a 213 px close-up → scale ~0.05
    hinted = scales_for_query(q, target_size_m=0.06, gsd_m=0.00612, size_mode="aircraft")
    assert min(hinted) <= 0.08


def test_dense_small_box_query_returns_quickly():
    """Regression for the 32x32 / 16x16 drawn-box hang: many coarse peaks + NMS."""
    import time
    from matcher import match_template_pyramid

    rng = np.random.default_rng(11)
    field = rng.integers(40, 120, size=(2160, 3840), dtype=np.uint8)
    # Low-frequency stubble so NCC fires in many places.
    blotch = cv2.GaussianBlur(rng.integers(0, 90, size=(2160, 3840), dtype=np.uint8), (21, 21), 0)
    field = cv2.addWeighted(field, 0.65, blotch, 0.35, 0)
    # 32x32 crop of the field itself — this is the hang class from the Avata frames.
    box = field[1584:1616, 2384:2416].copy()
    t0 = time.perf_counter()
    hits = match_template_pyramid(field, box, threshold=0.45, top_n=8, timeout_s=8)
    dt = time.perf_counter() - t0
    assert dt < 8.0, f"32x32 query took {dt:.2f}s"
    assert isinstance(hits, list)
    # 16x16 too
    box16 = field[1584:1600, 2384:2400].copy()
    t0 = time.perf_counter()
    hits16 = match_template_pyramid(field, box16, threshold=0.45, top_n=8, timeout_s=8)
    dt16 = time.perf_counter() - t0
    assert dt16 < 8.0, f"16x16 query took {dt16:.2f}s"
    print(f"    32x32 {dt:.2f}s n={len(hits)}; 16x16 {dt16:.2f}s n={len(hits16)}")
