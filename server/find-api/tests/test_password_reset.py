import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402


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


def _admin_client(client):
    accounts.create_user("BossAdmin", "admin-password-123", is_admin=True, mosaic_enabled=True)
    r = client.post("/auth/login", json={"username": "BossAdmin", "password": "admin-password-123"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _token_from(url):
    assert "/reset/#t=" in url
    return url.split("#t=", 1)[1]


def test_reset_flow_end_to_end(client):
    admin = _admin_client(client)
    accounts.create_user("Worker", "old-password-123")
    login = client.post("/auth/login", json={"username": "Worker", "password": "old-password-123"})
    old_bearer = {"Authorization": f"Bearer {login.json()['token']}"}
    r = client.post("/admin/users/Worker/reset-link", headers=admin)
    assert r.status_code == 200, r.text
    token = _token_from(r.json()["url"])
    assert r.json()["url"].startswith(config.PUBLIC_URL)
    # Stored hashed, never raw.
    conn = accounts._connect()
    rows = conn.execute("SELECT token_hash FROM password_resets").fetchall()
    assert all(row[0] != token for row in rows)
    # Old password still works until the reset completes.
    assert client.post("/auth/login", json={"username": "Worker", "password": "old-password-123"}).status_code == 200
    assert client.post("/auth/reset/check", json={"token": token}).json() == {"valid": True}
    # Too-short password does not burn the token.
    assert client.post("/auth/reset", json={"token": token, "new_password": "short"}).status_code == 400
    r = client.post("/auth/reset", json={"token": token, "new_password": "new-password-456"})
    assert r.status_code == 200, r.text
    # Old sessions are gone, old password no longer works, new one does.
    assert client.get("/auth/me", headers=old_bearer).json()["authenticated"] is False
    assert client.post("/auth/login", json={"username": "Worker", "password": "old-password-123"}).status_code == 401
    assert client.post("/auth/login", json={"username": "Worker", "password": "new-password-456"}).status_code == 200
    # Single use.
    again = client.post("/auth/reset", json={"token": token, "new_password": "another-password-789"})
    assert again.status_code == 400
    assert client.post("/auth/reset/check", json={"token": token}).json() == {"valid": False}


def test_reset_token_expires(client, monkeypatch):
    admin = _admin_client(client)
    accounts.create_user("Late", "old-password-123")
    token = _token_from(client.post("/admin/users/Late/reset-link", headers=admin).json()["url"])
    real = time.time
    monkeypatch.setattr(accounts.time, "time", lambda: real() + accounts.RESET_TTL + 5)
    r = client.post("/auth/reset", json={"token": token, "new_password": "new-password-456"})
    assert r.status_code == 400
    monkeypatch.setattr(accounts.time, "time", real)
    assert client.post("/auth/login", json={"username": "Late", "password": "old-password-123"}).status_code == 200


def test_new_link_replaces_old_one(client):
    admin = _admin_client(client)
    accounts.create_user("Twice", "old-password-123")
    t1 = _token_from(client.post("/admin/users/Twice/reset-link", headers=admin).json()["url"])
    t2 = _token_from(client.post("/admin/users/Twice/reset-link", headers=admin).json()["url"])
    assert client.post("/auth/reset", json={"token": t1, "new_password": "new-password-456"}).status_code == 400
    assert client.post("/auth/reset", json={"token": t2, "new_password": "new-password-456"}).status_code == 200


def test_request_does_not_reveal_accounts(client):
    accounts.create_user("RealOne", "old-password-123")
    a = client.post("/auth/reset/request", json={"username": "RealOne"})
    b = client.post("/auth/reset/request", json={"username": "NoSuchUser"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    admin = _admin_client(client)
    reqs = client.get("/admin/reset-requests", headers=admin).json()["requests"]
    assert [r["username"] for r in reqs] == ["RealOne"]
    # Making a link handles the request.
    client.post("/admin/users/RealOne/reset-link", headers=admin)
    assert client.get("/admin/reset-requests", headers=admin).json()["requests"] == []


def test_reset_endpoints_rate_limited(client):
    codes = [client.post("/auth/reset", json={"token": "x" * 40, "new_password": "new-password-456"}).status_code for _ in range(accounts.RESET_LIMIT + 2)]
    assert codes[-1] == 429
    codes = [client.post("/auth/reset/request", json={"username": "someone"}).status_code for _ in range(accounts.RESET_REQUEST_LIMIT + 1)]
    assert codes[-1] == 429


def test_reset_link_admin_only(client):
    accounts.create_user("Plain", "old-password-123")
    r = client.post("/auth/login", json={"username": "Plain", "password": "old-password-123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/admin/users/Plain/reset-link", headers=h).status_code == 403
    client.cookies.clear()
    assert client.post("/admin/users/Plain/reset-link").status_code == 401
