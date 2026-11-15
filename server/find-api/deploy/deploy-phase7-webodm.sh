#!/usr/bin/env bash
# Deploy Phase 7 hidden survey-quality processing (find-api + planner only).
# Does not replace the hosted APK, does not edit the Caddyfile, does not
# change public WebODM routes, and does not touch /var/www/apps.
# Run on the VPS as root:
#
#   sudo bash deploy-phase7-webodm.sh --yes /home/ubuntu/drone-mapper-phase7-src
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing to deploy. Re-run with --yes after review."
  exit 1
fi
shift
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0 --yes /path/to/src"
  exit 1
fi

SRC=${1:-/home/ubuntu/drone-mapper-phase7-src}
if [[ ! -f "$SRC/find-api/app.py" || ! -f "$SRC/find-api/processing.py" || ! -f "$SRC/find-api/webodm.py" || ! -f "$SRC/planner/index.html" ]]; then
  echo "ERROR: $SRC does not look like the Phase 7 tree."
  exit 1
fi
# The cancelled per-user WebODM linking approach must not be present.
if [[ -f "$SRC/find-api/mapsync.py" || -f "$SRC/find-api/maplink.py" ]]; then
  echo "ERROR: Per-user WebODM map-sync files are present. Refusing to deploy that approach."
  exit 1
fi

WWW=/var/www/drone-mapper
APPS=/var/www/apps
INSTALL_DIR=/opt/drone-mapper-find
DATA_DIR=/var/lib/drone-mapper-find
UNIT=/etc/systemd/system/drone-mapper-find.service
CRED=/home/ubuntu/private/drone-mapper-webodm.txt
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_ROOT=/var/backups
BACKUP_WWW=$BACKUP_ROOT/drone-mapper-page-$STAMP
BACKUP_API=$BACKUP_ROOT/drone-mapper-find-$STAMP
BACKUP_UNIT=$BACKUP_ROOT/drone-mapper-find.service-$STAMP
BACKUP_DB=$BACKUP_ROOT/drone-mapper-accounts-$STAMP.sqlite
EXPECT_APK_MIN=1000000

if [[ ! -d "$WWW" || ! -d "$INSTALL_DIR" || ! -f "$UNIT" ]]; then
  echo "ERROR: live drone-mapper paths missing"
  exit 1
fi
if [[ ! -f "$WWW/drone-mapper.apk" ]]; then
  echo "ERROR: live APK missing, aborting"
  exit 1
fi
if [[ ! -f "$CRED" ]]; then
  echo "ERROR: $CRED is missing. Create the internal service account first."
  exit 1
fi
APK_BEFORE=$(stat -c %s "$WWW/drone-mapper.apk")
APK_SHA_BEFORE=$(sha256sum "$WWW/drone-mapper.apk" | awk '{print $1}')
if [[ "$APK_BEFORE" -lt "$EXPECT_APK_MIN" ]]; then
  echo "ERROR: live APK size $APK_BEFORE looks wrong. Aborting."
  exit 1
fi
APPS_BEFORE=""
if [[ -d "$APPS" ]]; then
  APPS_BEFORE=$(find "$APPS" -type f -printf '%p %s\n' | sha256sum | awk '{print $1}')
fi

mkdir -p "$BACKUP_ROOT"
cp -a "$WWW" "$BACKUP_WWW"
echo "WWW backup: $BACKUP_WWW (APK $APK_BEFORE bytes)"

mkdir -p "$BACKUP_API"
rsync -a --exclude '.venv/' --exclude 'data/' --exclude '__pycache__/' "$INSTALL_DIR"/ "$BACKUP_API"/
echo "API backup: $BACKUP_API"

cp -a "$UNIT" "$BACKUP_UNIT"
echo "Unit backup: $BACKUP_UNIT"

if [[ -f "$DATA_DIR/accounts.sqlite" ]]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DATA_DIR/accounts.sqlite" "PRAGMA wal_checkpoint(FULL);" >/dev/null || true
  fi
  install -m 0600 -o root -g root "$DATA_DIR/accounts.sqlite" "$BACKUP_DB"
  if [[ -f "$DATA_DIR/accounts.sqlite-wal" ]]; then
    cp -a "$DATA_DIR/accounts.sqlite-wal" "$BACKUP_DB-wal"
  fi
  if [[ -f "$DATA_DIR/accounts.sqlite-shm" ]]; then
    cp -a "$DATA_DIR/accounts.sqlite-shm" "$BACKUP_DB-shm"
  fi
  echo "accounts.sqlite backup: $BACKUP_DB"
else
  echo "WARNING: accounts.sqlite not found in $DATA_DIR"
fi

# Overlay planner static files only. Never copy an APK. Never touch /var/www/apps.
install -m 0644 -o root -g caddy "$SRC/planner/index.html" "$WWW/index.html"
mkdir -p "$WWW/css" "$WWW/js"
for f in "$SRC/planner/css/"*; do
  [[ -f "$f" ]] || continue
  install -m 0644 -o root -g caddy "$f" "$WWW/css/$(basename "$f")"
done
for f in "$SRC/planner/js/"*; do
  [[ -f "$f" ]] || continue
  install -m 0644 -o root -g caddy "$f" "$WWW/js/$(basename "$f")"
done
for page in login signup settings admin; do
  if [[ -f "$SRC/planner/$page/index.html" ]]; then
    mkdir -p "$WWW/$page"
    install -m 0644 -o root -g caddy "$SRC/planner/$page/index.html" "$WWW/$page/index.html"
  fi
done

rsync -a \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SRC/find-api"/ "$INSTALL_DIR"/
# Keep hidden processing; still refuse leftover per-user link files.
rm -f "$INSTALL_DIR/mapsync.py" "$INSTALL_DIR/maplink.py" "$INSTALL_DIR/deploy/map-sync.env.example"
chown -R ubuntu:ubuntu "$INSTALL_DIR"
chmod 750 "$DATA_DIR"

# Ensure httpx is present in the existing venv.
sudo -u ubuntu "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

install -m 0644 "$INSTALL_DIR/deploy/drone-mapper-find.service" "$UNIT"
systemctl daemon-reload
systemctl restart drone-mapper-find.service
sleep 2
if ! curl -fsS http://127.0.0.1:8091/health | grep -q '"version":"0.5.5"'; then
  echo "ERROR: health check failed after restart. Rolling back." >&2
  systemctl --no-pager --full status drone-mapper-find.service || true
  rsync -a --exclude '.venv/' "$BACKUP_API"/ "$INSTALL_DIR"/
  cp -a "$BACKUP_UNIT" "$UNIT"
  rsync -a --exclude 'drone-mapper.apk' "$BACKUP_WWW"/ "$WWW"/
  systemctl daemon-reload
  systemctl restart drone-mapper-find.service
  exit 1
fi

APK_AFTER=$(stat -c %s "$WWW/drone-mapper.apk")
APK_SHA_AFTER=$(sha256sum "$WWW/drone-mapper.apk" | awk '{print $1}')
if [[ "$APK_AFTER" != "$APK_BEFORE" || "$APK_SHA_AFTER" != "$APK_SHA_BEFORE" ]]; then
  echo "ERROR: APK changed unexpectedly. Restore from $BACKUP_WWW." >&2
  exit 1
fi
if [[ -n "$APPS_BEFORE" ]]; then
  APPS_AFTER=$(find "$APPS" -type f -printf '%p %s\n' | sha256sum | awk '{print $1}')
  if [[ "$APPS_AFTER" != "$APPS_BEFORE" ]]; then
    echo "ERROR: /var/www/apps changed unexpectedly." >&2
    exit 1
  fi
fi

echo "Deploy finished."
echo "health: http://127.0.0.1:8091/health"
echo "backups: $BACKUP_WWW"
echo "         $BACKUP_API"
echo "         $BACKUP_UNIT"
echo "         $BACKUP_DB"
echo "APK left unchanged ($APK_AFTER bytes, sha256 $APK_SHA_AFTER)"
echo "Caddyfile was not edited. /var/www/apps was not touched."
