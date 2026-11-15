"""Outgoing email for Piletally (password reset links).

Settings come from a KEY=VALUE env file (default /var/lib/drone-mapper-find/smtp.env,
which systemd binds read-only from /home/ubuntu/private/piletally-smtp.env).
The file is re-read on every send so a new credential only needs a restart
when the bind mount itself was missing at service start.

Keys:
  SMTP_HOST       127.0.0.1 for the local Postfix, or e.g. smtp.gmail.com  (required)
  SMTP_PORT       25 (local), 587 (STARTTLS) or 465 (SSL)    (default 587)
  SMTP_USER       login name; leave out for the local Postfix (no login, no TLS)
  SMTP_PASSWORD   password; required only with SMTP_USER
  SMTP_STARTTLS   1 to force STARTTLS without a login         (default: only with a login)
  SMTP_FROM       From address                   (default SMTP_USER, else no-reply@piletally.com)
  SMTP_FROM_NAME  display name                   (default Piletally)
  SMTP_REPLY_TO   Reply-To address               (default support@piletally.com)

Nothing here ever logs or returns the password or the message body.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

NOT_CONFIGURED = "email not configured"
DEFAULT_ENV_FILE = "/var/lib/drone-mapper-find/smtp.env"
DEFAULT_REPLY_TO = "support@piletally.com"
TIMEOUT = 20


def env_file() -> Path:
    return Path(os.environ.get("FIND_API_SMTP_ENV", DEFAULT_ENV_FILE))


def load_settings() -> dict[str, str] | None:
    path = env_file()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    vals: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        vals[key] = value
    if not vals.get("SMTP_HOST"):
        return None
    if vals.get("SMTP_USER") and not vals.get("SMTP_PASSWORD"):
        return None
    return vals


def is_configured() -> bool:
    return load_settings() is not None


def _mask(addr: str) -> str:
    local, _, domain = addr.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def send_reset_email(to_addr: str, username: str, url: str, minutes: int) -> tuple[bool, str]:
    """Send the reset link. Returns (sent, status) where status is safe to log."""
    cfg = load_settings()
    if cfg is None:
        return False, NOT_CONFIGURED
    from_addr = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER") or "no-reply@piletally.com"
    msg = EmailMessage()
    msg["Subject"] = "Reset your Piletally password"
    msg["From"] = formataddr((cfg.get("SMTP_FROM_NAME") or "Piletally", from_addr))
    msg["To"] = to_addr
    msg["Reply-To"] = cfg.get("SMTP_REPLY_TO") or DEFAULT_REPLY_TO
    msg["Message-ID"] = make_msgid(domain=from_addr.partition("@")[2] or None)
    msg.set_content(
        f"Hi {username},\n\n"
        "Someone (hopefully you) asked to reset your Piletally password.\n\n"
        f"Open this link to pick a new password:\n{url}\n\n"
        f"The link works once and expires in {minutes} minutes.\n"
        "If you didn't ask for this, ignore this email. Your current password still works.\n\n"
        "Piletally\n"
    )
    host = cfg["SMTP_HOST"]
    try:
        port = int(cfg.get("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    ctx = ssl.create_default_context()
    user = cfg.get("SMTP_USER") or ""
    use_tls = bool(user) or cfg.get("SMTP_STARTTLS", "").lower() in {"1", "true", "yes"}
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx) as smtp:
                if user:
                    smtp.login(user, cfg["SMTP_PASSWORD"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=TIMEOUT) as smtp:
                if use_tls:
                    smtp.starttls(context=ctx)
                if user:
                    smtp.login(user, cfg["SMTP_PASSWORD"])
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return False, "email failed: login refused (check the app password)"
    except smtplib.SMTPRecipientsRefused:
        return False, "email failed: address refused"
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"email failed: {type(exc).__name__}"
    return True, f"email sent to {_mask(to_addr)}"
