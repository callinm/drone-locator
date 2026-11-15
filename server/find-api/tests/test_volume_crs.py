import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import volume_calc
import volume_crs
from georef import write_geotiff_4326


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "job-1160c8d1-dsm.tif"


def _cone_utm_bytes(tmp_path: Path) -> tuple[Path, dict]:
    """A real UTM-looking DSM with a cone pile and a proper easting/northing origin."""
    # Use a geographic GeoTIFF first? Better: write a UTM GeoTIFF via tifffile tags.
    import tifffile

    dsm = volume_calc.make_grid_dsm(80, 80, 0.5, -0.5, origin_x=500000.0, origin_y=4_000_040.0)
    dsm["z"].fill(10.0)
    volume_calc.add_cone(dsm, 500020.0, 4_000_020.0, 5.0, 4.0, plane={"a": 0, "b": 0, "c": 10.0})
    path = tmp_path / "utm-cone.tif"
    # EPSG:32616 (WGS 84 / UTM zone 16N)
    geokey = np.array(
        [1, 1, 0, 2, 3072, 0, 1, 32616, 1024, 0, 1, 1],
        dtype=np.uint16,
    )
    tifffile.imwrite(
        path,
        dsm["z"].astype(np.float32),
        photometric="minisblack",
        extratags=[
            (33550, 12, 3, [0.5, 0.5, 0.0], True),
            (33922, 12, 6, [0.0, 0.0, 0.0, 500000.0, 4_000_040.0, 0.0], True),
            (34735, 3, len(geokey), geokey, True),
        ],
    )
    loaded = volume_calc.load_dsm_geotiff(path.read_bytes())
    return path, loaded


def test_job_1160c8d1_lonlat_is_error_not_zero():
    if not FIXTURE.is_file():
        pytest.skip("job DSM fixture missing")
    dsm = volume_calc.load_dsm_geotiff(FIXTURE.read_bytes())
    assert volume_crs.is_local_frame(dsm)
    # The bounds previously reported by processing (lon/lat-like).
    pile = {
        "id": "pile-1",
        "name": "QA pile",
        "lat": -0.00021,
        "lon": -7.48849,
        "ring": [
            {"lat": -0.000338, "lon": -7.488642},
            {"lat": -0.000338, "lon": -7.488337},
            {"lat": -0.000085, "lon": -7.488337},
            {"lat": -0.000085, "lon": -7.488642},
        ],
    }
    with pytest.raises(volume_crs.RingError, match="no real-world GPS"):
        volume_crs.prepare_ring(dsm, pile)


def test_job_1160c8d1_local_ring_has_volume():
    if not FIXTURE.is_file():
        pytest.skip("job DSM fixture missing")
    dsm = volume_calc.load_dsm_geotiff(FIXTURE.read_bytes())
    # Elevated southern region in the local meter frame (same as the verified e2e).
    pile = {
        "id": "pile-1",
        "name": "QA pile",
        "lat": -39.75,
        "lon": 28.4,
        "frame": "local",
        "ring": [
            {"lat": -46.9, "lon": 0.0},
            {"lat": -46.9, "lon": 56.8},
            {"lat": -32.6, "lon": 56.8},
            {"lat": -32.6, "lon": 0.0},
        ],
    }
    ring = volume_crs.prepare_ring(dsm, pile)
    vol = volume_calc.measure_polygon(dsm, ring, 1.6)
    assert vol["ok"] if "ok" in vol else True
    assert vol["area_m2"] > 100
    assert vol["fill_m3"] > 100


def test_job_1160c8d1_pixel_ring_works():
    if not FIXTURE.is_file():
        pytest.skip("job DSM fixture missing")
    dsm = volume_calc.load_dsm_geotiff(FIXTURE.read_bytes())
    h, w = dsm["z"].shape
    pile = {
        "id": "pile-1",
        "name": "pixels",
        "lat": h * 0.85,
        "lon": w / 2,
        "frame": "pixel",
        "ring": [
            {"x": 0, "y": int(h * 0.7)},
            {"x": w, "y": int(h * 0.7)},
            {"x": w, "y": h},
            {"x": 0, "y": h},
        ],
    }
    ring = volume_crs.prepare_ring(dsm, pile)
    vol = volume_calc.measure_polygon(dsm, ring, 1.6)
    assert vol["area_m2"] > 50
    assert vol["fill_m3"] > 10


def test_georeferenced_utm_accepts_lonlat(tmp_path):
    path, dsm = _cone_utm_bytes(tmp_path)
    assert dsm.get("crs") in {"EPSG:32616", "EPSG:32616"}
    assert not volume_crs.is_local_frame(dsm)
    # Transform the cone center (500020, 4000020) to lon/lat and draw a ring.
    from pyproj import Transformer

    to_ll = Transformer.from_crs("EPSG:32616", "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(500020.0, 4_000_020.0)
    # ~8 m in degrees at this latitude
    d = 8.0 / 111_320.0
    pile = {
        "id": "pile-1",
        "name": "utm pile",
        "lat": lat,
        "lon": lon,
        "ring": [
            {"lat": lat - d, "lon": lon - d},
            {"lat": lat - d, "lon": lon + d},
            {"lat": lat + d, "lon": lon + d},
            {"lat": lat + d, "lon": lon - d},
        ],
    }
    ring = volume_crs.prepare_ring(dsm, pile)
    assert volume_crs.ring_overlaps(dsm, ring)
    vol = volume_calc.measure_polygon(dsm, ring, 1.6)
    expected = (1.0 / 3.0) * math.pi * 5.0 * 5.0 * 4.0
    assert vol["fill_m3"] > 0
    assert abs(vol["fill_m3"] - expected) / expected < 0.2


def test_geographic_dsm_uses_lonlat_directly(tmp_path):
    dsm = volume_calc.make_grid_dsm(50, 50, 0.00001, -0.00001, origin_x=-88.23, origin_y=40.09)
    dsm["z"].fill(0)
    volume_calc.add_cone(dsm, -88.22975, 40.08975, 0.00012, 4.0)
    dsm["crs"] = "EPSG:4326"
    # Persist and reload? not required if crs is set.
    pile = {
        "id": "p",
        "name": "geo",
        "lat": 40.08975,
        "lon": -88.22975,
        "ring": [
            {"lat": 40.0895, "lon": -88.23},
            {"lat": 40.0895, "lon": -88.2295},
            {"lat": 40.09, "lon": -88.2295},
            {"lat": 40.09, "lon": -88.23},
        ],
    }
    ring = volume_crs.prepare_ring(dsm, pile)
    vol = volume_calc.measure_polygon(dsm, ring)
    assert vol["area_m2"] > 0
