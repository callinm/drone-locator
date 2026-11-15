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

from tests.test_matcher import noisy_field  # noqa: E402


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


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _signup(client, username, password, website=""):
    body = {"username": username, "password": password}
    if website != "":
        body["website"] = website
    return client.post("/auth/signup", json=body)


def test_signup_and_me(client):
    r = _signup(client, "MapUser", "long-enough-password")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["username"] == "MapUser"
    assert body["user"]["is_admin"] is False
    assert body["user"]["mosaic_enabled"] is False
    assert body["token"]
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "MapUser"


def test_username_unique_case_insensitive(client):
    assert _signup(client, "rightway", "long-enough-password").status_code == 201
    r = _signup(client, "RightWay", "another-long-password")
    assert r.status_code == 409


def test_password_min_length(client):
    r = _signup(client, "shortpwd", "123456789")
    assert r.status_code == 400
    assert "10" in r.json()["detail"]


def test_honeypot_rejects_without_creating(client):
    r = _signup(client, "botuser", "long-enough-password", website="https://spam.example")
    assert r.status_code == 400
    # The username must still be available for a real person.
    ok = _signup(client, "botuser", "long-enough-password")
    assert ok.status_code == 201, ok.text


def test_signup_rate_limit(client):
    for i in range(accounts.SIGNUP_LIMIT):
        r = _signup(client, f"user{i:02d}", "long-enough-password")
        assert r.status_code == 201, r.text
        client.post("/auth/logout", headers={"X-CSRF-Token": r.json()["csrf"]})
    blocked = _signup(client, "user99", "long-enough-password")
    assert blocked.status_code == 429


def test_login_and_bad_password(client):
    _signup(client, "pilot", "long-enough-password")
    client.post("/auth/logout", headers={"Authorization": "Bearer " + client.cookies.get(accounts.SESSION_COOKIE, "")})
    # Cookie may still be present; use a fresh login.
    bad = client.post("/auth/login", json={"username": "pilot", "password": "wrong-password-here"})
    assert bad.status_code == 401
    good = client.post("/auth/login", json={"username": "PILOT", "password": "long-enough-password"})
    assert good.status_code == 200
    assert good.json()["user"]["username"] == "pilot"


def test_change_password(client):
    r = _signup(client, "changer", "old-password-ok")
    token = r.json()["token"]
    csrf = r.json()["csrf"]
    fail = client.post(
        "/auth/password",
        json={"current_password": "nope-nope-nope", "new_password": "new-password-ok"},
        headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
    )
    # Bearer skips CSRF, but current password must match.
    assert fail.status_code == 403
    ok = client.post(
        "/auth/password",
        json={"current_password": "old-password-ok", "new_password": "new-password-ok"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200, ok.text
    # Old token/session was revoked.
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["authenticated"] is False
    login = client.post("/auth/login", json={"username": "changer", "password": "new-password-ok"})
    assert login.status_code == 200


def test_delete_account_requires_password_and_removes_only_that_user(client):
    a = _signup(client, "alice", "alice-password")
    b = _signup(client, "bobby", "bobby-password")
    # After bobby signup, the test client holds bobby's cookie. Use bearer for alice.
    alice_token = a.json()["token"]
    # Enable mosaic for alice so we can plant a private picture, then delete.
    accounts.set_mosaic_enabled("alice", True)
    field = noisy_field(80, 100, seed=3)
    up = client.post(
        "/mosaics",
        files={"file": ("a.png", _png(field), "image/png")},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert up.status_code == 200, up.text
    mid = up.json()["id"]

    missing = client.post(
        "/auth/delete",
        json={"password": "wrong-password"},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert missing.status_code == 403

    gone = client.post(
        "/auth/delete",
        json={"password": "alice-password"},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert gone.status_code == 200
    assert gone.json()["deleted"] == "alice"
    assert accounts.get_user_by_username("alice") is None
    assert accounts.get_user_by_username("bobby") is not None
    # Alice's mosaic is gone.
    bob_token = b.json()["token"]
    accounts.set_mosaic_enabled("bobby", True)
    peek = client.get(f"/mosaics/{mid}", headers={"Authorization": f"Bearer {bob_token}"})
    assert peek.status_code == 404


def test_mosaic_requires_login_and_flag(client):
    field = noisy_field(80, 100, seed=4)
    anon = client.post("/mosaics", files={"file": ("f.png", _png(field), "image/png")})
    assert anon.status_code == 401

    r = _signup(client, "locked", "long-enough-password")
    token = r.json()["token"]
    locked = client.post(
        "/mosaics",
        files={"file": ("f.png", _png(field), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert locked.status_code == 403
    assert "locked" in locked.json()["detail"].lower()

    accounts.set_mosaic_enabled("locked", True)
    ok = client.post(
        "/mosaics",
        files={"file": ("f.png", _png(field), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200, ok.text


def test_mosaic_is_private_between_users(client):
    field = noisy_field(80, 100, seed=5)
    a = _signup(client, "ownerA", "long-enough-password")
    b = _signup(client, "ownerB", "long-enough-password")
    accounts.set_mosaic_enabled("ownerA", True)
    accounts.set_mosaic_enabled("ownerB", True)
    up = client.post(
        "/mosaics",
        files={"file": ("f.png", _png(field), "image/png")},
        headers={"Authorization": f"Bearer {a.json()['token']}"},
    )
    mid = up.json()["id"]
    peek = client.get(
        f"/mosaics/{mid}",
        headers={"Authorization": f"Bearer {b.json()['token']}"},
    )
    assert peek.status_code == 404
    own = client.get(
        f"/mosaics/{mid}",
        headers={"Authorization": f"Bearer {a.json()['token']}"},
    )
    assert own.status_code == 200


def test_non_admin_cannot_list_or_toggle(client):
    admin = accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    user_r = _signup(client, "rightway", "user-password-ok")
    token = user_r.json()["token"]
    listed = client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 403
    flip_self = client.post(
        "/admin/users/rightway/mosaic",
        json={"mosaic_enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert flip_self.status_code == 403
    flip_admin = client.post(
        "/admin/users/Callin/mosaic",
        json={"mosaic_enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert flip_admin.status_code == 403
    assert accounts.get_user_by_username("rightway").mosaic_enabled is False
    assert accounts.get_user_by_username("Callin").mosaic_enabled is True

    admin_session = accounts.create_session(admin)
    ok = client.post(
        "/admin/users/rightway/mosaic",
        json={"mosaic_enabled": True},
        headers={"Authorization": f"Bearer {admin_session.token}"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["mosaic_enabled"] is True


def test_admin_list_requires_csrf_for_cookie_posts(client):
    accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    login = client.post("/auth/login", json={"username": "Callin", "password": "admin-password-ok"})
    assert login.status_code == 200
    csrf = login.json()["csrf"]
    # Cookie is set; posting without CSRF must fail.
    bad = client.post("/admin/users/Callin/mosaic", json={"mosaic_enabled": False})
    assert bad.status_code == 403
    assert "CSRF" in bad.json()["detail"]
    good = client.post(
        "/admin/users/Callin/mosaic",
        json={"mosaic_enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert good.status_code == 200, good.text
    assert good.json()["user"]["mosaic_enabled"] is False


def test_anonymous_mosaic_is_unauthorized(client):
    # Fresh client without the tester Authorization header used in test_api.py
    r = client.post("/mosaics")
    assert r.status_code == 401


def test_admin_users_list_has_profile_fields_and_hides_secrets(client):
    admin = accounts.create_user("Callin", "admin-password-ok", is_admin=True, mosaic_enabled=True)
    _signup(client, "rightway", "user-password-ok")
    admin_session = accounts.create_session(admin)
    listed = client.get("/admin/users", headers={"Authorization": f"Bearer {admin_session.token}"})
    assert listed.status_code == 200, listed.text
    users = listed.json()["users"]
    names = {u["username"] for u in users}
    assert "Callin" in names and "rightway" in names
    for u in users:
        assert "password" not in u and "password_hash" not in u
        assert "email" in u
        assert "created_at" in u
        assert "last_login" in u
        assert "job_count" in u
        assert u.get("status") == "active"
        assert "credits" in u
        assert u["credits"] is None
        assert "features" in u
        assert "mosaic_enabled" in u["features"]
    # last_login is recorded when a session is created (signup + admin session).
    right = next(u for u in users if u["username"] == "rightway")
    assert right["last_login"] is not None
    # search / sort
    found = client.get(
        "/admin/users",
        params={"q": "rightway", "sort": "username", "direction": "asc"},
        headers={"Authorization": f"Bearer {admin_session.token}"},
    )
    assert found.status_code == 200
    assert [u["username"] for u in found.json()["users"]] == ["rightway"]


def test_login_updates_last_login(client):
    accounts.create_user("pat", "user-password-ok", is_admin=False, mosaic_enabled=False)
    before = accounts.get_user_by_username("pat").last_login
    assert before is None
    r = client.post("/auth/login", json={"username": "pat", "password": "user-password-ok"})
    assert r.status_code == 200
    after = accounts.get_user_by_username("pat").last_login
    assert after is not None
    assert before != after
