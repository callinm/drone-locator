import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402
import feedback  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    feedback.reset_rate_limits()
    import app as appmod

    with TestClient(appmod.app) as c:
        yield c
    accounts.reset_connection()
    accounts.reset_rate_limits()
    feedback.reset_rate_limits()


def _login(client, username, password):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    csrf = r.json().get("csrf") or client.cookies.get("dm_csrf")
    return {"x-csrf-token": csrf}


def test_feedback_requires_login(client):
    r = client.post("/feedback", json={"message": "hello"})
    assert r.status_code == 401


def test_feedback_submit_list_and_toggle(client):
    accounts.create_user("Pat", "user-password-ok", is_admin=False, mosaic_enabled=False)
    accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    headers = _login(client, "Pat", "user-password-ok")
    too_long = "x" * (feedback.MAX_LENGTH + 1)
    bad = client.post("/feedback", json={"message": too_long}, headers=headers)
    assert bad.status_code == 400
    empty = client.post("/feedback", json={"message": "   "}, headers=headers)
    assert empty.status_code == 400
    ok = client.post("/feedback", json={"message": "The volume number looked high."}, headers=headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()["feedback"]
    assert body["username"] == "Pat"
    assert body["message"] == "The volume number looked high."
    assert body["is_read"] is False

    # Regular user cannot list or toggle.
    assert client.get("/admin/feedback", headers=headers).status_code == 403

    admin = _login(client, "Callin", "admin-password-ok")
    listed = client.get("/admin/feedback", headers=admin)
    assert listed.status_code == 200
    items = listed.json()["feedback"]
    assert len(items) == 1
    assert items[0]["message"] == "The volume number looked high."
    fid = items[0]["id"]
    toggled = client.post(f"/admin/feedback/{fid}/read", json={"is_read": True}, headers=admin)
    assert toggled.status_code == 200
    assert toggled.json()["feedback"]["is_read"] is True
    again = client.post(f"/admin/feedback/{fid}/read", json={"is_read": False}, headers=admin)
    assert again.json()["feedback"]["is_read"] is False


def test_feedback_rate_limit(client):
    accounts.create_user("Pat", "user-password-ok")
    headers = _login(client, "Pat", "user-password-ok")
    for i in range(feedback.RATE_LIMIT):
        r = client.post("/feedback", json={"message": f"note {i}"}, headers=headers)
        assert r.status_code == 200, r.text
    blocked = client.post("/feedback", json={"message": "one more"}, headers=headers)
    assert blocked.status_code == 429


def test_admin_processing_panel(client):
    accounts.create_user("Pat", "user-password-ok")
    accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    user_headers = _login(client, "Pat", "user-password-ok")
    assert client.get("/admin/processing", headers=user_headers).status_code == 403
    admin = _login(client, "Callin", "admin-password-ok")
    r = client.get("/admin/processing", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    host = body["host"]
    assert "cpu_pct" in host
    assert host["ncpu"] >= 1
    assert host["ram_total_bytes"] > 0
    assert "disk" in host and host["disk"]["total_bytes"] > 0
    assert host["cpu_level"] in {"ok", "yellow", "red", "unknown"}
    assert host["ram_level"] in {"ok", "yellow", "red", "unknown"}
    assert isinstance(body["queue"], list)
    assert "running" in body
    assert "webodm" in body
    assert "history" in body
    # A second call should be able to fill cpu_pct from the /proc/stat delta.
    time.sleep(0.2)
    r2 = client.get("/admin/processing", headers=admin)
    assert r2.status_code == 200
    # cpu_pct may still be None on some hosts, but history should grow
    assert isinstance(r2.json()["history"], list)
