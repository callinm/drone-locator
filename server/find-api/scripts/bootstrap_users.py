#!/usr/bin/env python3
"""Create the Callin (admin) and rightway accounts if they are missing.

Writes generated passwords to a 0600 file and never prints them.
Safe to re-run: existing users keep their passwords; admin/mosaic flags
are reconciled to the intended values.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import accounts  # noqa: E402


USERS = (
    {
        "username": "Callin",
        "is_admin": True,
        "mosaic_enabled": True,
        "role": "admin",
        "mosaic_label": "yes",
    },
    {
        "username": "rightway",
        "is_admin": False,
        "mosaic_enabled": False,
        "role": "user",
        "mosaic_label": "no",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Drone Mapper accounts.")
    parser.add_argument(
        "--logins-path",
        default="/home/ubuntu/private/drone-mapper-logins.txt",
        help="0600 file that will hold generated passwords (existing users are not rotated).",
    )
    args = parser.parse_args()
    accounts._connect()

    created_entries = []
    names = []
    for spec in USERS:
        existing = accounts.get_user_by_username(spec["username"])
        if existing:
            accounts.ensure_user(
                spec["username"],
                accounts.generate_password(),  # unused when the user already exists
                is_admin=spec["is_admin"],
                mosaic_enabled=spec["mosaic_enabled"],
            )
            names.append(spec["username"])
            continue
        password = accounts.generate_password()
        accounts.create_user(
            spec["username"],
            password,
            is_admin=spec["is_admin"],
            mosaic_enabled=spec["mosaic_enabled"],
        )
        created_entries.append(
            {
                "username": spec["username"],
                "password": password,
                "role": spec["role"],
                "mosaic_enabled": spec["mosaic_label"],
            }
        )
        names.append(spec["username"])

    logins_path = Path(args.logins_path)
    if created_entries:
        # If the file already exists, append a dated section rather than
        # wiping passwords written on a previous run.
        if logins_path.is_file():
            accounts.write_login_file(
                logins_path,
                _merge_existing(logins_path, created_entries),
            )
        else:
            accounts.write_login_file(logins_path, created_entries)
    elif not logins_path.is_file():
        # No new passwords and no file — leave a short note without secrets.
        logins_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(logins_path.parent, 0o700)
        fd = os.open(str(logins_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(
                "# Drone Mapper logins\n"
                "# Both seed users already existed; passwords were not rotated.\n"
                "# Usernames: Callin, rightway\n"
            )
        os.chmod(logins_path, 0o600)

    print("usernames: " + ", ".join(names))
    print("logins_path: " + str(logins_path))
    print("created: " + str(len(created_entries)))
    return 0


def _merge_existing(path: Path, new_entries: list[dict]) -> list[dict]:
    """Keep previously stored logins for usernames we did not just create."""
    import re

    text = path.read_text()
    blocks = re.split(r"\n(?=username: )", text)
    kept: list[dict] = []
    new_names = {e["username"].lower() for e in new_entries}
    for block in blocks:
        m_user = re.search(r"^username:\s*(.+)$", block, re.M)
        m_pass = re.search(r"^password:\s*(.+)$", block, re.M)
        m_role = re.search(r"^role:\s*(.+)$", block, re.M)
        m_mos = re.search(r"^mosaic_enabled:\s*(.+)$", block, re.M)
        if not m_user or not m_pass:
            continue
        name = m_user.group(1).strip()
        if name.lower() in new_names:
            continue
        kept.append(
            {
                "username": name,
                "password": m_pass.group(1).strip(),
                "role": (m_role.group(1).strip() if m_role else "user"),
                "mosaic_enabled": (m_mos.group(1).strip() if m_mos else "no"),
            }
        )
    return kept + new_entries


if __name__ == "__main__":
    raise SystemExit(main())
