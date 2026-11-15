"""Timing benchmark on the largest feasible grayscale mosaic (up to 20k×20k)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import tifffile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from matcher import search  # noqa: E402
from tests.test_matcher import make_target  # noqa: E402

OUT = ROOT / "data" / "bench"
OUT.mkdir(parents=True, exist_ok=True)


def write_mosaic(path: Path, size: int, paste_xy: tuple[int, int], target: np.ndarray) -> None:
    """Stream a size×size uint8 mosaic to a BigTIFF so we don't hold two copies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row_chunk = 512
    mm = tifffile.memmap(path, shape=(size, size), dtype=np.uint8, bigtiff=True)
    for y in range(0, size, row_chunk):
        h = min(row_chunk, size - y)
        # Cheap structured noise: hashed coordinates, not a full RNG buffer of 20k.
        ys = np.arange(y, y + h, dtype=np.uint32)[:, None]
        xs = np.arange(size, dtype=np.uint32)[None, :]
        tile = ((ys * 131 + xs * 17 + (ys // 64) * 9 + (xs // 64) * 13) & 63).astype(np.uint8) + 40
        mm[y : y + h] = tile
    px, py = paste_xy
    th, tw_ = target.shape
    mm[py : py + th, px : px + tw_] = target
    mm.flush()
    del mm


def run(size: int = 20000) -> dict:
    target = make_target(64)
    px, py = 12345 if size >= 12400 else size // 2, 8765 if size >= 9000 else size // 3
    # Keep the paste fully inside.
    px = min(px, size - target.shape[1] - 8)
    py = min(py, size - target.shape[0] - 8)
    path = OUT / f"mosaic_{size}.tif"
    t0 = time.perf_counter()
    if not path.exists() or path.stat().st_size < size * size:
        print(f"writing {size}×{size} mosaic…")
        write_mosaic(path, size, (px, py), target)
    write_s = time.perf_counter() - t0
    print(f"open memmap {path} ({path.stat().st_size / 1e6:.1f} MB)")
    mosaic = tifffile.memmap(path, mode="r")
    t1 = time.perf_counter()
    hits = search(mosaic, target, top_n=5, threshold=0.4)
    elapsed = time.perf_counter() - t1
    result = {
        "size": size,
        "pixels": size * size,
        "write_s": write_s,
        "match_s": elapsed,
        "hits": [
            {
                "x": h.x,
                "y": h.y,
                "w": h.w,
                "h": h.h,
                "score": h.score,
                "method": h.method,
            }
            for h in hits
        ],
        "expected": {"x": px, "y": py, "w": int(target.shape[1]), "h": int(target.shape[0])},
    }
    if hits:
        cx, cy = hits[0].x + hits[0].w / 2, hits[0].y + hits[0].h / 2
        ex, ey = px + target.shape[1] / 2, py + target.shape[0] / 2
        result["center_error_px"] = float(np.hypot(cx - ex, cy - ey))
        result["ok"] = result["center_error_px"] <= 20
    else:
        result["center_error_px"] = None
        result["ok"] = False
    print(result)
    return result


if __name__ == "__main__":
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    run(size)
