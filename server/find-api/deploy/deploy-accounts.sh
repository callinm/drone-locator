#!/usr/bin/env bash
# Deploy Drone Mapper accounts + planner pages and bootstrap seed users.
# Run on the VPS as root after the tree has been copied to
# /home/ubuntu/drone-mapper-accounts-src (or pass the path).
#
#   sudo bash deploy-accounts.sh --yes [/path/to/src]
#
# Does not touch WebODM, Quantum Stockpile, Kalshi, or the Caddyfile.
# The hosted APK is only replaced if a new drone-mapper.apk is in the
# tarball AND --apk is also passed.
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing to deploy. Re-run with --yes after review."
  exit 1
fi
shift
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0 --yes"
  exit 1
fi

REPLACE_APK=0
SRC=""
for arg in "$@"; do
  if [[ "$arg" == "--apk" ]]; then
    REPLACE_APK=1
  else
    SRC="$arg"
  fi
done
SRC=${SRC:-/home/ubuntu/drone-mapper-accounts-src}
if [[ ! -d "$SRC" ]]; then
  echo "ERROR: source tree missing: $SRC"
  exit 1
fi
if [[ ! -f "$SRC/find-api/app.py" || ! -f "$SRC/planner/index.html" ]]; then
  echo "ERROR: $SRC does not look like the drone-mapper tree."
  exit 1
fi

WWW=/var/www/drone-mapper
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_WWW=/var/backups/drone-mapper-page-$STAMP
BACKUP_API=/var/backups/drone-mapper-find-$STAMP
INSTALL_DIR=/opt/drone-mapper-find
DATA_DIR=/var/lib/drone-mapper-find
LOGINS=/home/ubuntu/private/drone-mapper-logins.txt
EXPECT_APK_MIN=1000000

if [[ ! -d "$WWW" ]]; then
  echo "ERROR: $WWW missing"
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

mkdir -p /var/backups
cp -a "$WWW" "$BACKUP_WWW"
echo "WWW backup: $BACKUP_WWW (APK $APK_BEFORE bytes)"
if [[ -d "$INSTALL_DIR" ]]; then
  mkdir -p "$BACKUP_API"
  rsync -a --exclude '.venv/' --exclude 'data/' "$INSTALL_DIR"/ "$BACKUP_API"/
  echo "API backup: $BACKUP_API"
fi

# Overlay planner static files, including new account pages.
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
  mkdir -p "$WWW/$page"
  install -m 0644 -o root -g caddy "$SRC/planner/$page/index.html" "$WWW/$page/index.html"
done

if [[ "$REPLACE_APK" -eq 1 && -f "$SRC/drone-mapper.apk" ]]; then
  install -m 0644 -o root -g caddy "$SRC/drone-mapper.apk" "$WWW/drone-mapper.apk"
  echo "APK replaced ($(stat -c %s "$WWW/drone-mapper.apk") bytes)"
else
  # Restore the original APK if a previous copy step could not have changed it;
  # we never copy an APK in the static overlay above.
  echo "APK left unchanged ($(stat -c %s "$WWW/drone-mapper.apk") bytes)"
fi

# Refresh the API sources and unit file, keep the data dir.
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
rsync -a --delete \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SRC/find-api"/ "$INSTALL_DIR"/
chown -R ubuntu:ubuntu "$INSTALL_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  sudo -u ubuntu python3 -m venv "$INSTALL_DIR/.venv"
fi
sudo -u ubuntu "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u ubuntu "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

install -m 0644 "$INSTALL_DIR/deploy/drone-mapper-find.service" /etc/systemd/system/drone-mapper-find.service
systemctl daemon-reload
systemctl enable drone-mapper-find.service
systemctl restart drone-mapper-find.service
sleep 2
if ! curl -fsS http://127.0.0.1:8091/health | grep -q '"ok"'; then
  echo "ERROR: health check failed after restart" >&2
  systemctl --no-pager --full status drone-mapper-find.service || true
  exit 1
fi
echo "API health ok"

# Bootstrap seed users. Runs outside the systemd sandbox so it can write
# the 0600 logins file under /home/ubuntu/private.
install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/private
sudo -u ubuntu env FIND_API_DATA="$DATA_DIR" \
  "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/scripts/bootstrap_users.py" \
  --logins-path "$LOGINS"
# bootstrap prints only usernames and the path, never passwords.
chmod 0600 "$LOGINS" 2>/dev/null || true
chown ubuntu:ubuntu "$LOGINS" 2>/dev/null || true

APK_AFTER=$(stat -c %s "$WWW/drone-mapper.apk")
if [[ "$REPLACE_APK" -eq 0 && "$APK_AFTER" != "$APK_BEFORE" ]]; then
  echo "ERROR: APK size changed unexpectedly ($APK_BEFORE -> $APK_AFTER). Restore from $BACKUP_WWW." >&2
  exit 1
fi

echo "Deploy finished."
echo "signup: https://wheresthepelican.com/drone-mapper/signup/"
echo "settings: https://wheresthepelican.com/drone-mapper/settings/"
echo "admin: https://wheresthepelican.com/drone-mapper/admin/"
echo "logins_path: $LOGINS"
