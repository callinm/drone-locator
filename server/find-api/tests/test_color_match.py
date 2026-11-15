import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from color_match import (  # noqa: E402
    extract_dominant_colors,
    fuse_matches,
    match_color,
    ranges_for_request,
    search_with_color,
)
from matcher import Match, match_template_pyramid, search as shape_search

OUT = Path("/workspace/drone-mapper/out/color-match-hit-rates.json")
DRONE_TOP = Path("/workspace/drone_top.jpg")


def green_brown_field(h: int, w: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # BGR field: greens and browns
    g = rng.integers(60, 140, size=(h, w), dtype=np.uint8)
    r = rng.integers(40, 90, size=(h, w), dtype=np.uint8)
    b = rng.integers(20, 70, size=(h, w), dtype=np.uint8)
    img = np.dstack([b, g, r])
    blotch = cv2.GaussianBlur(rng.integers(0, 50, size=(h, w), dtype=np.uint8), (41, 41), 0)
    img = cv2.addWeighted(img, 0.85, cv2.cvtColor(blotch, cv2.COLOR_GRAY2BGR), 0.35, 0)
    return img


def synthetic_drone(size: int = 48, color=(40, 40, 220), plus=(20, 220, 220)) -> np.ndarray:
    """A compact aircraft-like glyph: fuselage + arms, in BGR."""
    img = np.zeros((size, size, 3), np.uint8)
    c = size // 2
    cv2.rectangle(img, (c - 4, 6), (c + 4, size - 6), color, -1)
    cv2.rectangle(img, (6, c - 3), (size - 6, c + 3), color, -1)
    cv2.circle(img, (c, 10), 5, plus, -1)
    return img


def paste_color(dest: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    h, w = src.shape[:2]
    dest[y : y + h, x : x + w] = src


def occlude(img: np.ndarray, frac: float = 0.3) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    out[0 : int(h * frac), :, :] = 0
    return out


def hit(hits, exp_cx, exp_cy, tol=22):
    for i, m in enumerate(hits):
        cx, cy = m.x + m.w / 2, m.y + m.h / 2
        if abs(cx - exp_cx) <= tol and abs(cy - exp_cy) <= tol:
            return i, m
    return None, None


def gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


CASES = []


def _record(name, found_shape, found_color, found_fused, score=None):
    CASES.append(
        {
            "name": name,
            "shape": bool(found_shape),
            "color": bool(found_color),
            "color+shape": bool(found_fused),
            "score": score,
        }
    )


@pytest.mark.parametrize(
    "angle,occluded",
    [(0, False), (25, False), (40, False), (0, True), (30, True)],
)
def test_color_finds_red_drone_in_field(angle, occluded):
    field = green_brown_field(800, 1100, seed=4 + int(angle))
    drone = synthetic_drone(48, color=(36, 36, 215), plus=(30, 210, 230))
    h, w = drone.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    rot = cv2.warpAffine(drone, M, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)
    if occluded:
        rot = occlude(rot, 0.32)
    x0, y0 = 640, 260
    paste_color(field, rot, x0, y0)
    exp_cx, exp_cy = x0 + rot.shape[1] / 2, y0 + rot.shape[0] / 2

    shape_hits = shape_search(gray(field), gray(drone), top_n=8, threshold=0.4)
    i_s, _ = hit(shape_hits, exp_cx, exp_cy)
    color_hits = match_color(
        field,
        ranges=ranges_for_request(preset="red", reference=drone),
        top_n=8,
        query_wh=(48, 48),
    )
    i_c, m_c = hit(color_hits, exp_cx, exp_cy, tol=28)
    fused = search_with_color(gray(field), gray(drone), field, drone, mode="color+shape", top_n=8, preset="red")
    i_f, m_f = hit(fused, exp_cx, exp_cy, tol=28)
    _record(
        f"red angle={angle} occluded={occluded}",
        i_s is not None,
        i_c is not None,
        i_f is not None,
        None if m_f is None else m_f.score,
    )
    # Color (or fused) must find the red aircraft even when rotation/occlusion weaken shape.
    assert i_c is not None or i_f is not None, f"color missed angle={angle} occluded={occluded}"
    if i_f is not None:
        assert fused[i_f].method in {"color", "color+shape", "shape"}


def test_white_preset_and_fuse_methods():
    field = green_brown_field(700, 900, seed=9)
    drone = synthetic_drone(40, color=(230, 230, 230), plus=(200, 200, 200))
    x0, y0 = 200, 300
    paste_color(field, drone, x0, y0)
    fused = search_with_color(gray(field), gray(drone), field, drone, mode="color+shape", top_n=6, preset="white")
    i, m = hit(fused, x0 + 20, y0 + 20, tol=24)
    assert i is not None
    assert m.method in {"color", "color+shape", "shape"}


def test_dominant_colors_prefer_aircraft_paint():
    swatch = np.zeros((80, 80, 3), np.uint8)
    swatch[:] = (30, 40, 30)  # dark border
    swatch[20:60, 20:60] = (40, 50, 210)  # red body
    colors = extract_dominant_colors(swatch, k=3)
    assert colors
    # The red body should be among the top colors (BGR, red channel high).
    assert any(c[2] > c[1] + 40 and c[2] > c[0] + 40 for c in colors[:3])


def test_fuse_assigns_methods():
    shape = [Match(x=10, y=10, w=40, h=40, score=0.7, method="shape")]
    color = [Match(x=14, y=12, w=36, h=38, score=0.6, method="color")]
    extra = [Match(x=400, y=400, w=30, h=30, score=0.55, method="color")]
    out = fuse_matches(shape, color + extra, top_n=5)
    methods = {m.method for m in out}
    assert "color+shape" in methods
    assert "color" in methods


def test_real_drone_photo_pasted_in_field():
    if not DRONE_TOP.is_file():
        pytest.skip("drone_top.jpg not available")
    src = cv2.imread(str(DRONE_TOP))
    assert src is not None
    # Use a tight central crop so the aircraft fills the query, then scale to a mosaic footprint.
    h, w = src.shape[:2]
    crop = src[int(h * 0.25) : int(h * 0.75), int(w * 0.25) : int(w * 0.75)]
    query = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA)
    placed = cv2.resize(crop, (72, 72), interpolation=cv2.INTER_AREA)
    field = green_brown_field(900, 1200, seed=11)
    x0, y0 = 800, 150
    paste_color(field, placed, x0, y0)
    exp_cx, exp_cy = x0 + 36, y0 + 36
    shape_hits = shape_search(gray(field), gray(query), top_n=8, threshold=0.35)
    i_s, _ = hit(shape_hits, exp_cx, exp_cy, tol=30)
    fused = search_with_color(gray(field), gray(query), field, query, mode="color+shape", top_n=8)
    i_f, m_f = hit(fused, exp_cx, exp_cy, tol=30)
    _record("real drone_top.jpg in field", i_s is not None, i_f is not None, i_f is not None, None if m_f is None else m_f.score)
    assert i_f is not None, "fused search missed a real reference photo pasted into a field"



def _luminance_matched_blob(size=40, field_gray=90, bgr=(30, 40, 230)):
    """A flat colored disc whose grayscale value matches the field, so shape is weak."""
    img = np.zeros((size, size, 3), np.uint8)
    # After paste we'll rely on color; build a disc of the given BGR.
    cv2.circle(img, (size // 2, size // 2), size // 2 - 1, bgr, -1)
    return img


def test_color_recovers_when_gray_shape_fails():
    """Several same-size gray discs plus one red stain of equal luminance.

    Shape matching has no unique outline, so its top hit is unreliable.
    Color ranking should put the red aircraft-colored stain first.
    """
    field = np.full((800, 1100, 3), (40, 110, 55), dtype=np.uint8)
    rng = np.random.default_rng(3)
    noise = rng.integers(0, 16, size=field.shape, dtype=np.uint8)
    field = cv2.add(field, noise)
    decoy_bgr = (70, 105, 55)  # also near field gray, no red paint
    decoys = [(120, 120), (400, 500), (800, 200), (900, 600), (200, 600)]
    for (x, y) in decoys:
        disc = _luminance_matched_blob(44, bgr=decoy_bgr)
        disc = cv2.GaussianBlur(disc, (9, 9), 0)
        field[y:y + 44, x:x + 44] = disc
    target = (0, 50, 215)
    blob = cv2.GaussianBlur(_luminance_matched_blob(44, bgr=target), (9, 9), 0)
    x0, y0 = 560, 220
    field[y0:y0 + 44, x0:x0 + 44] = blob
    query = _luminance_matched_blob(40, bgr=(10, 45, 230))
    exp_cx, exp_cy = x0 + 22, y0 + 22

    shape_hits = shape_search(gray(field), gray(query), top_n=8, threshold=0.3)
    # "Before": is the TOP shape hit the red stain?
    shape_top = False
    if shape_hits:
        m = shape_hits[0]
        shape_top = abs((m.x + m.w / 2) - exp_cx) <= 24 and abs((m.y + m.h / 2) - exp_cy) <= 24
    color_only = search_with_color(gray(field), gray(query), field, query, mode="color", top_n=8, preset="red")
    i_c, _ = hit(color_only, exp_cx, exp_cy, tol=24)
    color_top = i_c == 0
    fused = search_with_color(gray(field), gray(query), field, query, mode="color+shape", top_n=8, preset="red")
    i_f, m_f = hit(fused, exp_cx, exp_cy, tol=24)
    fused_top = i_f == 0
    _record(
        "red stain among gray decoys (top-1)",
        shape_top,
        color_top,
        fused_top,
        None if m_f is None else m_f.score,
    )
    print("top-1 shape", shape_top, "color", color_top, "fused", fused_top, "n_shape", len(shape_hits), "n_color", len(color_only))
    assert color_top or fused_top
    assert i_c is not None or i_f is not None


def test_write_hit_rate_summary():
    # This runs after the parametrized tests in the same file when collected together.
    if not CASES:
        pytest.skip("no cases recorded in this process")
    shape_n = sum(1 for c in CASES if c["shape"])
    color_n = sum(1 for c in CASES if c["color"] or c["color+shape"])
    total = len(CASES)
    summary = {
        "total_cases": total,
        "shape_hits": shape_n,
        "shape_rate": shape_n / total,
        "color_or_fused_hits": color_n,
        "color_or_fused_rate": color_n / total,
        "cases": CASES,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2))
    print("hit rates", json.dumps({k: summary[k] for k in ("total_cases", "shape_hits", "shape_rate", "color_or_fused_hits", "color_or_fused_rate")}))
    assert color_n >= shape_n
    assert color_n / total >= 0.8


def _dull_blue_fin(w=8, h=3, paint=(170, 85, 25), bg=(108, 80, 63)):
    """A tiny painted fin after JPEG/blend dulling (A-frame style)."""
    img = np.zeros((h, w, 3), np.uint8)
    # Most of the fin is the dulled centre color; a few pixels stay closer to paint.
    img[:] = bg
    img[0, 3:6] = paint
    img[1, 2:7] = (int(0.4 * paint[0] + 0.6 * bg[0]), int(0.4 * paint[1] + 0.6 * bg[1]), int(0.4 * paint[2] + 0.6 * bg[2]))
    return img


def test_small_marking_finds_tiny_dulled_blue_fin():
    field = green_brown_field(900, 1400, seed=21)
    fin = _dull_blue_fin()
    x0, y0 = 700, 440
    field[y0 : y0 + fin.shape[0], x0 : x0 + fin.shape[1]] = fin
    # Close-up reference of the designed paint, much larger than the on-mosaic fin.
    closeup = np.zeros((99, 213, 3), np.uint8)
    closeup[:] = (30, 30, 30)
    closeup[20:80, 20:193] = (170, 85, 25)
    exp_cx, exp_cy = x0 + fin.shape[1] / 2, y0 + fin.shape[0] / 2

    # Aircraft defaults should still be allowed to miss (this is the bug).
    aircraft = search_with_color(
        gray(field), gray(closeup), field, closeup,
        mode="color", top_n=8, hex_color="#1955AA", size_mode="aircraft",
    )
    i_air, _ = hit(aircraft, exp_cx, exp_cy, tol=16)

    marked = search_with_color(
        gray(field), gray(closeup), field, closeup,
        mode="color", top_n=8, hex_color="#1955AA",
        size_mode="small_marking", target_size_m=0.06, gsd_m=0.00612,
    )
    i_m, m = hit(marked, exp_cx, exp_cy, tol=16)
    fp = len(marked) if i_m is None else max(0, len(marked) - 1)
    print(f"    small marking hit rank={i_m} n={len(marked)} fp={fp} aircraft_hit={i_air is not None}")
    assert i_m is not None, f"small-marking color missed the 8x3 fin; n={len(marked)}"
    # Keep false positives manageable among top_n.
    assert fp <= 7


def test_small_marking_scales_passed_to_shape():
    from matcher import scales_for_query
    q = np.zeros((213, 99), np.uint8)
    s = scales_for_query(q, size_mode="small_marking")
    assert min(s) < 0.6


def test_tight_band_ranks_fin_and_control_is_clean():
    """Tight HSV hits the paint; the loose field-wide band must not fill top_n."""
    from color_match import range_sets_for_request, match_color, hex_to_bgr, search_with_color

    field = green_brown_field(900, 1400, seed=21)
    fin = _dull_blue_fin()
    x0, y0 = 700, 440
    field[y0 : y0 + fin.shape[0], x0 : x0 + fin.shape[1]] = fin
    closeup = np.zeros((99, 213, 3), np.uint8)
    closeup[:] = (30, 30, 30)
    closeup[20:80, 20:193] = (170, 85, 25)
    exp_cx, exp_cy = x0 + fin.shape[1] / 2, y0 + fin.shape[0] / 2
    tight, loose = range_sets_for_request(hex_color="#1955AA")
    # Operational tight is 0.5.3's first loose=True band (not the even-stricter loose=False band).
    assert tight == [((90, 76, 68), (126, 255, 255))]
    assert loose and loose[0] == ((88, 26, 54), (128, 163, 190))

    color = search_with_color(
        gray(field), gray(closeup), field, closeup,
        mode="color", top_n=8, hex_color="#1955AA", size_mode="small_marking",
    )
    i, m = hit(color, exp_cx, exp_cy, tol=16)
    assert i is not None and i <= 2, f"color-only missed or ranked {i}"
    # Control (no fin) should not return a crowd of mid-score field blobs.
    control = green_brown_field(900, 1400, seed=21)
    ctl = search_with_color(
        gray(control), gray(closeup), control, closeup,
        mode="color", top_n=8, hex_color="#1955AA", size_mode="small_marking",
    )
    strong = [h for h in ctl if h.score >= 0.70]
    assert len(strong) == 0, f"control had {len(strong)} strong color hits"

    fused = search_with_color(
        gray(field), gray(closeup), field, closeup,
        mode="color+shape", top_n=8, hex_color="#1955AA", size_mode="small_marking",
    )
    i_f, _ = hit(fused, exp_cx, exp_cy, tol=16)
    assert i_f is not None and i_f <= 2, f"color+shape ranked the fin at {i_f}"


def test_target_size_px_without_gsd():
    from matcher import scales_for_query
    from color_match import expected_target_px

    assert expected_target_px(None, 0.06, target_size_px=8) == 8
    assert expected_target_px(None, 0.06) is None
    q = np.zeros((213, 99), np.uint8)
    hinted = scales_for_query(q, target_size_px=8, size_mode="aircraft")
    assert min(hinted) <= 0.05
