import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402

# Isolate storage for tests (fresh dir so leftover mosaics cannot trip MAX_MOSAICS).
config.DATA_DIR = ROOT / "data" / "test-store"
config.TTL_SECONDS = 3600

from app import app  # noqa: E402
from tests.test_matcher import make_target, noisy_field, paste  # noqa: E402


import accounts  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    with TestClient(app) as c:
        user = accounts.create_user(
            "tester", "test-password-ok", is_admin=False, mosaic_enabled=True
        )
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        yield c
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_upload_and_match_pasted_target(client):
    target = make_target(48)
    field = noisy_field(700, 900, seed=11)
    x0, y0 = 430, 180
    paste(field, target, x0, y0)
    up = client.post(
        "/mosaics",
        files={"file": ("field.png", _png(field), "image/png")},
        data={"west": "-88.2264", "south": "40.0830", "east": "-88.2240", "north": "40.0850"},
    )
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["width"] == 900 and body["height"] == 700
    assert body["georeferenced"] is True
    mid = body["id"]

    hit = client.post(
        f"/mosaics/{mid}/match",
        files={"query": ("target.png", _png(target), "image/png")},
        data={"top_n": "5"},
    )
    assert hit.status_code == 200, hit.text
    matches = hit.json()["matches"]
    assert matches, "expected a match"
    m = matches[0]
    assert m["score"] >= 0.5
    cx, cy = m["x"] + m["w"] / 2, m["y"] + m["h"] / 2
    assert abs(cx - (x0 + 24)) <= 12
    assert abs(cy - (y0 + 24)) <= 12
    assert m["lat"] is not None and m["lon"] is not None
    # lon/lat should lie inside the given bounds
    assert -88.2264 <= m["lon"] <= -88.2240
    assert 40.0830 <= m["lat"] <= 40.0850
    print(f"    api match: score={m['score']:.3f} box=({m['x']:.1f},{m['y']:.1f},{m['w']:.1f},{m['h']:.1f}) lonlat=({m['lon']:.6f},{m['lat']:.6f})")


def test_match_by_bbox(client):
    target = make_target(48)
    field = noisy_field(600, 800, seed=13)
    x0, y0 = 200, 250
    paste(field, target, x0, y0)
    up = client.post("/mosaics", files={"file": ("f.png", _png(field), "image/png")})
    mid = up.json()["id"]
    # Query is a slightly padded crop of the pasted region (as if the user clicked it).
    r = client.post(
        f"/mosaics/{mid}/match",
        data={"x": str(x0 - 2), "y": str(y0 - 2), "w": "52", "h": "52", "top_n": "3"},
    )
    assert r.status_code == 200, r.text
    m = r.json()["matches"][0]
    cx, cy = m["x"] + m["w"] / 2, m["y"] + m["h"] / 2
    assert abs(cx - (x0 + 24)) <= 12
    assert abs(cy - (y0 + 24)) <= 12


def test_rejects_non_image(client):
    r = client.post("/mosaics", files={"file": ("note.txt", b"hello world", "text/plain")})
    assert r.status_code == 400


def test_unknown_id(client):
    r = client.get("/mosaics/notarealid")
    assert r.status_code == 404


def test_preview_jpeg(client):
    field = noisy_field(120, 160, seed=2)
    up = client.post("/mosaics", files={"file": ("f.png", _png(field), "image/png")})
    assert up.status_code == 200, up.text
    mid = up.json()["id"]
    r = client.get(f"/mosaics/{mid}/preview")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content[:3] == b"\xff\xd8\xff"


def test_top_n_allows_100_and_clamps_above(client):
    import config
    assert config.TOP_N_MAX == 100
    field = noisy_field(200, 240, seed=3)
    up = client.post("/mosaics", files={"file": ("f.png", _png(field), "image/png")})
    mid = up.json()["id"]
    r = client.post(
        f"/mosaics/{mid}/match",
        data={"x": "10", "y": "10", "w": "32", "h": "32", "top_n": "100", "method": "shape"},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("top_n") == 100
    r2 = client.post(
        f"/mosaics/{mid}/match",
        data={"x": "10", "y": "10", "w": "32", "h": "32", "top_n": "500", "method": "shape"},
    )
    assert r2.status_code == 200
    assert r2.json().get("top_n") == 100


def test_low_altitude_is_skipped_and_flagged(client):
    field = noisy_field(240, 320, seed=4)
    up = client.post("/mosaics", files={"file": ("f.png", _png(field), "image/png")})
    mid = up.json()["id"]
    r = client.post(
        f"/mosaics/{mid}/match",
        data={
            "x": "10", "y": "10", "w": "16", "h": "16",
            "method": "color", "color": "#1955AA",
            "size_mode": "small_marking", "target_size_m": "0.06",
            "gsd_m": "0.0002", "altitude_m": "0.5",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is True
    assert body["skip_reason"] == "low_altitude"
    assert body["matches"] == []


def test_batch_match_summary(client):
    field = noisy_field(180, 220, seed=5)
    ids = []
    for i in range(2):
        up = client.post("/mosaics", files={"file": (f"f{i}.png", _png(field), "image/png")})
        assert up.status_code == 200, up.text
        ids.append(up.json()["id"])
    q = _png(field[10:40, 10:40])
    r = client.post(
        "/mosaics/batch-match",
        files={"query": ("q.png", q, "image/png")},
        data={
            "mosaic_ids": ",".join(ids),
            "method": "color",
            "color": "#1955AA",
            "size_mode": "small_marking",
            "top_n": "8",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["frames"] == 2
    assert body["summary"]["processed"] == 2
    assert len(body["results"]) == 2
