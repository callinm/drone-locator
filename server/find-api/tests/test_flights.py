import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts
import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    import app as appmod

    with TestClient(appmod.app) as c:
        user = accounts.create_user("Mapper", "long-enough-password", mosaic_enabled=True)
        session = accounts.create_session(user)
        c.headers["Authorization"] = f"Bearer {session.token}"
        yield c
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _mission():
    return {
        "format": "drone-mapper-mission",
        "name": "Field",
        "computed": {"photoCount": 40, "lineCount": 4},
        "settings": {"altitudeM": 50, "speedMps": 5},
        "waypoints": [{"lat": 40.0, "lon": -88.0, "altM": 50} for _ in range(8)],
    }


def test_start_stop_and_flying_now(client):
    created = client.post("/sync/missions", json={"mission": _mission(), "name": "Field"})
    assert created.status_code == 201, created.text
    mid = created.json()["id"]
    start = client.post(f"/sync/missions/{mid}/flights")
    assert start.status_code == 201, start.text
    flight = start.json()
    assert flight["flying"] is True
    assert flight["mission_id"] == mid

    active = client.get("/sync/flights/active")
    assert active.status_code == 200
    assert any(f["id"] == flight["id"] for f in active.json()["flying_now"])

    changes = client.get("/sync/changes?since=0")
    assert changes.status_code == 200
    assert any(f["id"] == flight["id"] and f["flying"] for f in changes.json()["flying_now"])

    img = Image.new("RGB", (16, 12), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    up = client.post(
        f"/sync/flights/{flight['id']}/media",
        files={"file": ("snap.jpg", buf.getvalue(), "image/jpeg")},
        data={"kind": "snapshot"},
    )
    assert up.status_code == 200, up.text
    assert up.json()["media"][0]["kind"] == "snapshot"
    mid_media = up.json()["media"][0]["id"]
    got = client.get(f"/sync/media/{mid_media}")
    assert got.status_code == 200
    assert got.content[:2] == b"\xff\xd8"

    stopped = client.post(f"/sync/flights/{flight['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["flying"] is False
    assert stopped.json()["status"] == "ended"
    assert client.get("/sync/flights/active").json()["flying_now"] == []


def test_unknown_mission_rejected(client):
    r = client.post("/sync/missions/not-a-real-mission-id/flights")
    assert r.status_code in (400, 404)
