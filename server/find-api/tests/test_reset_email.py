import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402
import config  # noqa: E402
import mailer  # noqa: E402


class FakeSMTP:
    sent = []
    logins = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, pw):
        if FakeSMTP.fail_login:
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"no")
        FakeSMTP.logins.append(user)

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", store)
    monkeypatch.setenv("FIND_API_SMTP_ENV", str(tmp_path / "missing.env"))
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail_login = [], [], False
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    accounts.reset_connection()
    accounts.reset_rate_limits()
    import app as appmod

    with TestClient(appmod.app) as c:
        yield c
    accounts.reset_connection()
    accounts.reset_rate_limits()


def _configure(tmp_path, monkeypatch, port="587"):
    env = tmp_path / "smtp.env"
    env.write_text(
        "SMTP_HOST=smtp.example.test\nSMTP_PORT=%s\nSMTP_USER=sender@example.test\n"
        "SMTP_PASSWORD='abcd efgh ijkl mnop'\nSMTP_FROM=support@piletally.com\n" % port
    )
    monkeypatch.setenv("FIND_API_SMTP_ENV", str(env))


def _admin(client):
    accounts.create_user("BossAdmin", "admin-password-123", is_admin=True, mosaic_enabled=True)
    r = client.post("/auth/login", json={"username": "BossAdmin", "password": "admin-password-123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _log(client, admin):
    return client.get("/admin/reset-requests", headers=admin).json()


def test_not_configured_keeps_request_open(client):
    accounts.create_user("Ann", "old-password-123", email="ann@example.test")
    r = client.post("/auth/reset/request", json={"username": "Ann"})
    assert r.status_code == 200
    admin = _admin(client)
    body = _log(client, admin)
    assert body["email_configured"] is False
    assert [(x["username"], x["status"]) for x in body["requests"]] == [("Ann", "email not configured")]
    assert body["log"][0]["status"] == "email not configured"
    # No token was made.
    assert accounts._connect().execute("SELECT COUNT(*) FROM password_resets").fetchone()[0] == 0
    # Manual fallback still works and clears the box.
    assert client.post("/admin/users/Ann/reset-link", headers=admin).status_code == 200
    body = _log(client, admin)
    assert body["requests"] == []
    assert body["log"][0]["status"].startswith("admin made a link")


def test_no_email_on_account(client, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    accounts.create_user("Bob", "old-password-123")
    client.post("/auth/reset/request", json={"username": "Bob"})
    admin = _admin(client)
    assert _log(client, admin)["requests"][0]["status"] == "no email on account"
    assert FakeSMTP.sent == []


def test_sends_link_and_logs_without_token(client, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    accounts.create_user("Cara", "old-password-123", email="cara@example.test")
    a = client.post("/auth/reset/request", json={"username": "Cara"})
    b = client.post("/auth/reset/request", json={"username": "Nobody"})
    assert a.json() == b.json() and a.status_code == b.status_code == 200
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "cara@example.test"
    assert "support@piletally.com" in msg["From"]
    assert FakeSMTP.logins == ["sender@example.test"]
    text = msg.get_content()
    url = next(line for line in text.splitlines() if "/reset/#t=" in line).strip()
    token = url.split("#t=", 1)[1]
    assert url.startswith(config.PUBLIC_URL)
    admin = _admin(client)
    body = _log(client, admin)
    assert body["requests"] == []  # emailed -> leaves the to-do box
    entry = body["log"][0]
    assert entry["username"] == "Cara" and entry["status"] == "email sent to c***@example.test"
    assert token not in str(body)
    db = accounts._connect()
    assert token not in str([tuple(r) for r in db.execute("SELECT * FROM password_reset_requests")])
    # Link works once, 30 minutes.
    assert client.post("/auth/reset", json={"token": token, "new_password": "new-password-456"}).status_code == 200
    assert client.post("/auth/reset", json={"token": token, "new_password": "new-password-789"}).status_code == 400
    # Second ask within 5 minutes doesn't send another email.
    client.post("/auth/reset/request", json={"username": "Cara"})
    assert len(FakeSMTP.sent) == 1
    assert _log(client, admin)["log"][0]["status"].startswith("skipped")


def test_send_failure_revokes_token_and_stays_open(client, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, port="465")
    FakeSMTP.fail_login = True
    accounts.create_user("Dee", "old-password-123", email="dee@example.test")
    client.post("/auth/reset/request", json={"username": "Dee"})
    admin = _admin(client)
    reqs = _log(client, admin)["requests"]
    assert reqs[0]["status"].startswith("email failed: login refused")
    db = accounts._connect()
    assert db.execute("SELECT COUNT(*) FROM password_resets WHERE used_at IS NULL").fetchone()[0] == 0


def test_email_on_signup_and_settings(client):
    r = client.post("/auth/signup", json={"username": "Eve123", "password": "pass-word-1234", "email": "eve@example.test"})
    assert r.status_code == 201 and r.json()["user"]["email"] == "eve@example.test"
    bad = client.post("/auth/signup", json={"username": "Eve124", "password": "pass-word-1234", "email": "nope"})
    assert bad.status_code == 400
    ok = client.post("/auth/signup", json={"username": "Eve125", "password": "pass-word-1234"})
    assert ok.status_code == 201 and ok.json()["user"]["email"] is None
    tok = ok.json()["token"]
    csrf = ok.json()["csrf"]
    h = {"Authorization": f"Bearer {tok}", "X-CSRF-Token": csrf}
    r = client.post("/auth/email", json={"email": "  new@example.test "}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == "new@example.test"
    assert client.post("/auth/email", json={"email": "bad@"}, headers=h).status_code == 400
    assert client.post("/auth/email", json={"email": ""}, headers=h).json()["user"]["email"] is None
    client.cookies.clear()
    assert client.post("/auth/email", json={"email": "x@example.test"}).status_code == 401


def test_local_postfix_no_login(client, tmp_path, monkeypatch):
    env = tmp_path / "smtp.env"
    env.write_text(
        "SMTP_HOST=127.0.0.1\nSMTP_PORT=25\nSMTP_FROM=no-reply@piletally.com\n"
        "SMTP_FROM_NAME=Piletally\nSMTP_REPLY_TO=support@piletally.com\n"
    )
    monkeypatch.setenv("FIND_API_SMTP_ENV", str(env))
    accounts.create_user("Fay", "old-password-123", email="fay@example.test")
    client.post("/auth/reset/request", json={"username": "Fay"})
    assert FakeSMTP.logins == []
    msg = FakeSMTP.sent[0]
    assert msg["From"] == "Piletally <no-reply@piletally.com>"
    assert msg["Reply-To"] == "support@piletally.com"
