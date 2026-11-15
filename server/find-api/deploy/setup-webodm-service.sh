#!/usr/bin/env bash
# Create the single internal WebODM service account and project used by
# find-api. Does not modify existing WebODM users or other projects, does
# not print the password, and does not change Caddy.
# Run on the VPS as root:
#   sudo bash setup-webodm-service.sh --yes
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing to create the service account. Re-run with --yes after review."
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0 --yes"
  exit 1
fi

CRED_DIR=/home/ubuntu/private
CRED=$CRED_DIR/drone-mapper-webodm.txt
mkdir -p "$CRED_DIR"
chown ubuntu:ubuntu "$CRED_DIR"
chmod 700 "$CRED_DIR"

if [[ -f "$CRED" ]]; then
  echo "Credentials file already exists (not overwritten)."
  echo "Service account setup skipped."
  exit 0
fi

if ! docker inspect webapp >/dev/null 2>&1; then
  echo "ERROR: WebODM webapp container is not running. Start it with: docker start db webapp worker"
  exit 1
fi

PASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
WORK=$(mktemp -d)
chmod 700 "$WORK"
# Quote the password safely for a Python string.
PASS_PY=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$PASS")
cat > "$WORK/setup.py" << PY
from django.contrib.auth.models import User
from app.models import Project
username = "dronemapper-service"
password = $PASS_PY
user, created = User.objects.get_or_create(username=username, defaults={"email": "dronemapper-service@localhost"})
user.set_password(password)
user.is_staff = False
user.is_superuser = False
user.is_active = True
user.save()
project = Project.objects.filter(owner=user, name="Drone Mapper").first()
if project is None:
    project = Project.objects.create(owner=user, name="Drone Mapper")
try:
    from guardian.shortcuts import assign_perm
    for perm in ("view_project", "add_project", "change_project", "delete_project"):
        try:
            assign_perm(perm, user, project)
        except Exception:
            pass
except Exception:
    pass
# Normal WebODM users belong to the Default group, which has model
# permissions for tasks and view access to the processing node.
from django.contrib.auth.models import Group
try:
    default_group = Group.objects.get(name="Default")
    user.groups.add(default_group)
except Group.DoesNotExist:
    pass
print("DM_PROJECT_ID=" + str(project.id))
PY

OUT=$(docker exec -i webapp python manage.py shell < "$WORK/setup.py")
PROJECT_ID=$(printf '%s\n' "$OUT" | sed -n 's/^DM_PROJECT_ID=//p' | tail -1)
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: could not create the service project." >&2
  rm -rf "$WORK"
  exit 1
fi

umask 077
cat > "$CRED" << EOF
# Internal Drone Mapper WebODM service account. Do not share.
# find-api talks to WebODM only on loopback.
WEBODM_BASE_URL=http://127.0.0.1:8001
WEBODM_USERNAME=dronemapper-service
WEBODM_PASSWORD=$PASS
WEBODM_PROJECT_ID=$PROJECT_ID
EOF
chown ubuntu:ubuntu "$CRED"
chmod 600 "$CRED"
unset PASS PASS_PY
rm -rf "$WORK"

python3 - "$CRED" << 'PY'
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
need = ("WEBODM_BASE_URL=", "WEBODM_USERNAME=", "WEBODM_PASSWORD=", "WEBODM_PROJECT_ID=")
if any(k not in text for k in need):
    raise SystemExit("credentials file incomplete")
print("Service account ready. Project id is recorded. Credentials file is 0600.")
PY
