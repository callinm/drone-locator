#!/usr/bin/env bash
# Deploy Drone Mapper dashboard/app sync (server + website only).
# Does not replace the hosted APK, touch WebODM, change logins, or
# rewrite the Caddyfile. Run on the VPS as root:
#
#   sudo bash deploy-dashboard-sync.sh --yes /home/ubuntu/drone-mapper-sync-src
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

SRC=${1:-/home/ubuntu/drone-mapper-sync-src}
if [[ ! -f "$SRC/find-api/app.py" || ! -f "$SRC/find-api/dashsync.py" || ! -f "$SRC/planner/index.html" ]]; then
  echo "ERROR: $SRC does not look like the dashboard-sync tree."
  exit 1
fi
if [[ -f "$SRC/find-api/mapsync.py" || -f "$SRC/find-api/maplink.py" || -f "$SRC/find-api/webodm.py" ]]; then
  echo "ERROR: WebODM map-sync files are present. Refusing to deploy that approach."
  exit 1
fi

WWW=/var/www/drone-mapper
INSTALL_DIR=/opt/drone-mapper-find
DATA_DIR=/var/lib/drone-mapper-find
UNIT=/etc/systemd/system/drone-mapper-find.service
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
APK_BEFORE=$(stat -c %s "$WWW/drone-mapper.apk")
if [[ "$APK_BEFORE" -lt "$EXPECT_APK_MIN" ]]; then
  echo "ERROR: live APK size $APK_BEFORE looks wrong. Aborting."
  exit 1
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
  # Flush WAL into a consistent copy.
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

# Overlay planner static files. Never copy an APK.
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

# Refresh API sources; keep venv and the data dir (accounts + mosaics).
rsync -a \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SRC/find-api"/ "$INSTALL_DIR"/
# Guarantee WebODM linking files are not left behind if a previous attempt added them.
rm -f "$INSTALL_DIR/mapsync.py" "$INSTALL_DIR/maplink.py" "$INSTALL_DIR/webodm.py" \
  "$INSTALL_DIR/deploy/map-sync.env.example"
chown -R ubuntu:ubuntu "$INSTALL_DIR"
chmod 750 "$DATA_DIR"

# Unit file is unchanged for this release, but reinstall from the tree so the
# backup and the live copy stay aligned. Restart the API only.
install -m 0644 "$INSTALL_DIR/deploy/drone-mapper-find.service" "$UNIT"
systemctl daemon-reload
systemctl restart drone-mapper-find.service
sleep 2
if ! curl -fsS http://127.0.0.1:8091/health | grep -q '"ok"'; then
  echo "ERROR: health check failed after restart. Rolling back." >&2
  systemctl --no-pager --full status drone-mapper-find.service || true
  rsync -a --exclude '.venv/' "$BACKUP_API"/ "$INSTALL_DIR"/
  cp -a "$BACKUP_UNIT" "$UNIT"
  rsync -a --exclude 'drone-mapper.apk' "$BACKUP_WWW"/ "$WWW"/
  # Keep the original APK if the backup copy somehow differed; never delete it.
  systemctl daemon-reload
  systemctl restart drone-mapper-find.service
  exit 1
fi

APK_AFTER=$(stat -c %s "$WWW/drone-mapper.apk")
if [[ "$APK_AFTER" != "$APK_BEFORE" ]]; then
  echo "ERROR: APK size changed unexpectedly ($APK_BEFORE -> $APK_AFTER). Restore the APK from $BACKUP_WWW." >&2
  exit 1
fi

echo "Deploy finished."
echo "health: http://127.0.0.1:8091/health"
echo "backups: $BACKUP_WWW"
echo "         $BACKUP_API"
echo "         $BACKUP_UNIT"
echo "         $BACKUP_DB"
echo "APK left unchanged ($APK_AFTER bytes)"
