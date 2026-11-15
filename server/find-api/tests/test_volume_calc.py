import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import volume_calc


def test_cone_volume_matches_analytical():
    # 40x40 grid, 0.5 m cells, cone radius 5 m, height 4 m at center.
    dsm = volume_calc.make_grid_dsm(80, 80, 0.5, 0.5, origin_x=0, origin_y=0)
    # initialize ground to zero plane
    dsm["z"].fill(0)
    cx, cy, radius, height = 20.0, 20.0, 5.0, 4.0
    volume_calc.add_cone(dsm, cx, cy, radius, height)
    ring = [
        {"x": cx - 6, "y": cy - 6},
        {"x": cx + 6, "y": cy - 6},
        {"x": cx + 6, "y": cy + 6},
        {"x": cx - 6, "y": cy + 6},
    ]
    vol = volume_calc.measure_polygon(dsm, ring, 1.6)
    expected = (1.0 / 3.0) * math.pi * radius * radius * height
    # Prismoidal sum on a 0.5 m grid is a close approximation.
    assert vol["fill_m3"] > 0
    assert abs(vol["fill_m3"] - expected) / expected < 0.08
    assert abs(vol["tons"] - vol["fill_m3"] * 1.6) < 1e-6
