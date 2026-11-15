import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402


PASSWORD = "long-enough-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    import app as appmod

    with TestClient(appmod.app) as c:
        yield c
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _mission(name="Sync test survey", photos=4, lines=2):
    return {
        "format": "drone-mapper-mission",
        "version": 1,
        "aircraft": "Mavic Air 2",
        "createdAt": "2026-11-12T18:00:00-06:00",
        "name": name,
        "notes": "",
        "polygon": [{"lat": 41.0, "lon": -93.0}, {"lat": 41.001, "lon": -93.0}, {"lat": 41.001, "lon": -92.999}, {"lat": 41.0, "lon": -92.999}],
        "settings": {"altitudeM": 40, "frontOverlap": 0.7, "sideOverlap": 0.6, "speedMps": 6},
        "computed": {"photoCount": photos, "lineCount": lines, "lineSpacingM": 10, "altitudeM": 40},
        "waypoints": [],
        "photos": [],
    }


def _signup(client, username):
    r = client.post("/auth/signup", json={"username": username, "password": PASSWORD, "website": ""})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    csrf = r.json()["csrf"]
    return token, csrf


def _auth_headers(token, csrf, cookie=False):
    if cookie:
        return {"X-CSRF-Token": csrf}
    return {"Authorization": f"Bearer {token}"}


def test_sync_requires_session(client):
    for method, path in [
        ("GET", "/sync/missions"),
        ("GET", "/sync/changes"),
        ("POST", "/sync/missions"),
        ("GET", "/sync/settings"),
        ("PUT", "/sync/settings"),
        ("GET", "/sync/find-results"),
    ]:
        r = client.request(method, path)
        assert r.status_code == 401, (path, r.status_code, r.text)


def test_save_list_and_owner_isolation(client):
    token_a, csrf_a = _signup(client, "aliceSync")
    # New client session for bob so cookies do not mix when we also test cookie auth.
    token_b, csrf_b = _signup(client, "bobSync")

    ha = _auth_headers(token_a, csrf_a)
    hb = _auth_headers(token_b, csrf_b)

    created = client.post(
        "/sync/missions",
        headers=ha,
        json={"id": "alice-mission-01", "mission": _mission("Alice field")},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"] == "alice-mission-01"
    assert body["name"] == "Alice field"
    assert body["mission"]["format"] == "drone-mapper-mission"
    assert body["deleted"] is False
    assert body["version"] == 1
    assert body["updated_at"]

    listed = client.get("/sync/missions", headers=ha)
    assert listed.status_code == 200
    ids = [m["id"] for m in listed.json()["missions"]]
    assert ids == ["alice-mission-01"]

    bob_list = client.get("/sync/missions", headers=hb)
    assert bob_list.status_code == 200
    assert bob_list.json()["missions"] == []

    # Bob cannot read or overwrite Alice's mission.
    assert client.get("/sync/missions/alice-mission-01", headers=hb).status_code == 404
    steal = client.put(
        "/sync/missions/alice-mission-01",
        headers=hb,
        json={"mission": _mission("Stolen")},
    )
    assert steal.status_code == 404
    # Alice still owns the original name.
    got = client.get("/sync/missions/alice-mission-01", headers=ha)
    assert got.status_code == 200
    assert got.json()["name"] == "Alice field"

    # Bob can save his own mission.
    bob_save = client.post(
        "/sync/missions",
        headers=hb,
        json={"mission": _mission("Bob field")},
    )
    assert bob_save.status_code == 201
    bob_id = bob_save.json()["id"]
    assert client.get(f"/sync/missions/{bob_id}", headers=ha).status_code == 404
    assert client.get(f"/sync/missions/{bob_id}", headers=hb).status_code == 200


def test_rename_soft_delete_and_restore(client):
    token, csrf = _signup(client, "caraSync")
    h = _auth_headers(token, csrf)
    client.post("/sync/missions", headers=h, json={"id": "cara-mission-01", "mission": _mission("Old name")})

    renamed = client.patch("/sync/missions/cara-mission-01", headers=h, json={"name": "New name"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "New name"
    assert renamed.json()["mission"]["name"] == "New name"
    assert renamed.json()["version"] == 2

    deleted = client.delete("/sync/missions/cara-mission-01", headers=h)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["deleted_at"]

    # Default list hides tombstones.
    listed = client.get("/sync/missions", headers=h)
    assert listed.json()["missions"] == []

    # Incremental sync still sees the tombstone so clients can drop it.
    changes = client.get("/sync/changes?since=0", headers=h)
    assert changes.status_code == 200
    tombstones = [m for m in changes.json()["missions"] if m["id"] == "cara-mission-01"]
    assert len(tombstones) == 1
    assert tombstones[0]["deleted"] is True
    assert "mission" not in tombstones[0]

    # Full get with include_deleted path (get allows include_deleted=True internally).
    got = client.get("/sync/missions/cara-mission-01", headers=h)
    assert got.status_code == 200
    assert got.json()["deleted"] is True

    restored = client.patch("/sync/missions/cara-mission-01", headers=h, json={"restore": True})
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted"] is False
    assert restored.json()["name"] == "New name"
    listed = client.get("/sync/missions", headers=h)
    assert [m["id"] for m in listed.json()["missions"]] == ["cara-mission-01"]


def test_last_write_wins_and_progress(client):
    token, csrf = _signup(client, "drewSync")
    h = _auth_headers(token, csrf)
    first = client.put(
        "/sync/missions/drew-mission-01",
        headers=h,
        json={"mission": _mission("v1", photos=10, lines=5)},
    )
    assert first.status_code == 200 or first.status_code == 201 or first.status_code == 200
    # PUT is not declared as 201
    assert first.status_code == 200, first.text
    v1 = first.json()["updated_at"]

    second = client.put(
        "/sync/missions/drew-mission-01",
        headers=h,
        json={"mission": _mission("v2", photos=12, lines=6)},
    )
    assert second.status_code == 200
    assert second.json()["name"] == "v2"
    assert second.json()["updated_at"] >= v1
    assert second.json()["version"] == 2

    progress = client.put(
        "/sync/missions/drew-mission-01/progress",
        headers=h,
        json={
            "status": "in_progress",
            "lines_done": 2,
            "lines_total": 6,
            "photos_done": 4,
            "photos_total": 12,
            "last_lat": 41.01,
            "last_lon": -93.01,
        },
    )
    assert progress.status_code == 200, progress.text
    body = progress.json()
    assert body["status"] == "in_progress"
    assert body["lines_done"] == 2
    assert body["photos_done"] == 4

    listed = client.get("/sync/missions", headers=h)
    item = listed.json()["missions"][0]
    assert item["progress"]["lines_done"] == 2
    assert item["progress"]["status"] == "in_progress"

    # Completed
    done = client.put(
        "/sync/missions/drew-mission-01/progress",
        headers=h,
        json={"status": "completed", "lines_done": 6, "lines_total": 6, "photos_done": 12, "photos_total": 12},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "completed"


def test_progress_and_settings_are_owner_isolated(client):
    token_a, csrf_a = _signup(client, "erinSync")
    token_b, csrf_b = _signup(client, "frankSync")
    ha = _auth_headers(token_a, csrf_a)
    hb = _auth_headers(token_b, csrf_b)
    client.put("/sync/missions/erin-mission-01", headers=ha, json={"mission": _mission("Erin")})
    client.put("/sync/settings", headers=ha, json={"settings": {"altitudeM": 50, "preset": "stockpile"}})
    client.put(
        "/sync/missions/erin-mission-01/progress",
        headers=ha,
        json={"status": "paused", "lines_done": 1, "lines_total": 2},
    )

    assert client.get("/sync/settings", headers=hb).json()["settings"] is None
    assert client.put(
        "/sync/missions/erin-mission-01/progress",
        headers=hb,
        json={"status": "completed"},
    ).status_code == 404
    mine = client.get("/sync/settings", headers=ha)
    assert mine.json()["settings"]["altitudeM"] == 50


def test_settings_and_find_results_roundtrip(client):
    token, csrf = _signup(client, "ginaSync")
    h = _auth_headers(token, csrf)
    saved = client.put("/sync/settings", headers=h, json={"settings": {"altitudeM": 40, "speedMps": 6}})
    assert saved.status_code == 200, saved.text
    got = client.get("/sync/settings", headers=h)
    assert got.json()["settings"]["speedMps"] == 6

    fr = client.put(
        "/sync/find-results",
        headers=h,
        json={"results": {"matches": [{"score": 0.9, "lat": 41.0, "lon": -93.0}], "mosaic_id": "abc"}},
    )
    assert fr.status_code == 200, fr.text
    again = client.get("/sync/find-results", headers=h)
    assert again.json()["results"]["mosaic_id"] == "abc"


def test_cookie_write_requires_csrf(client):
    token, csrf = _signup(client, "hankSync")
    # TestClient keeps cookies from signup. A write without CSRF must fail.
    r = client.post("/sync/missions", json={"mission": _mission("No csrf")})
    assert r.status_code == 403
    ok = client.post(
        "/sync/missions",
        headers={"X-CSRF-Token": csrf},
        json={"mission": _mission("With csrf")},
    )
    assert ok.status_code == 201, ok.text


def test_rejects_non_mission_payload(client):
    token, csrf = _signup(client, "ivySync")
    h = _auth_headers(token, csrf)
    bad = client.post("/sync/missions", headers=h, json={"mission": {"name": "nope"}})
    assert bad.status_code == 400
    assert "drone-mapper" in bad.json()["detail"]


def test_changes_since_includes_new_mission_only_after_cutoff(client):
    token, csrf = _signup(client, "judeSync")
    h = _auth_headers(token, csrf)
    first = client.post("/sync/missions", headers=h, json={"id": "jude-one-01", "mission": _mission("One")})
    t1 = first.json()["updated_at"]
    empty = client.get(f"/sync/changes?since={t1 + 0.001}", headers=h)
    assert empty.json()["missions"] == []
    client.put("/sync/missions/jude-two-01", headers=h, json={"mission": _mission("Two")})
    later = client.get(f"/sync/changes?since={t1 + 0.001}", headers=h)
    ids = [m["id"] for m in later.json()["missions"]]
    assert "jude-two-01" in ids
    assert "jude-one-01" not in ids
