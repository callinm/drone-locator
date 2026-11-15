#!/usr/bin/env bash
# Install the Find My Drone API on this VPS.
# Intended to be copied to the server and run THERE (so $ is safe).
# Do NOT run this until Callin has approved go-live.
#
#   sudo bash /opt/drone-mapper-find/deploy/deploy.sh --yes
#
# This script:
#   - creates /opt/drone-mapper-find and /var/lib/drone-mapper-find
#   - builds a Python venv and installs requirements
#   - installs the systemd unit and starts the service on 127.0.0.1:8091
#   - does NOT edit the Caddyfile (prints the snippet and the exact
#     insertion point instead, so the operator can review first)
#
set -euo pipefail

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing to install. Re-run with --yes after Callin has approved go-live."
  echo "This will install a systemd unit and start a localhost service; Caddy is left untouched."
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0 --yes"
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR=/opt/drone-mapper-find
DATA_DIR=/var/lib/drone-mapper-find
SERVICE=drone-mapper-find.service

echo "Installing from $SRC_DIR -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
# Copy the service sources but never clobber a live data dir or a venv mid-run.
rsync -a --delete \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SRC_DIR"/ "$INSTALL_DIR"/

chown -R ubuntu:ubuntu "$INSTALL_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  sudo -u ubuntu python3 -m venv "$INSTALL_DIR/.venv"
fi
sudo -u ubuntu "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u ubuntu "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

install -m 0644 "$INSTALL_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 1
systemctl --no-pager --full status "$SERVICE" || true

if curl -fsS http://127.0.0.1:8091/health; then
  echo
  echo "Service is healthy on 127.0.0.1:8091."
else
  echo "WARNING: health check failed. See journalctl -u $SERVICE" >&2
fi

echo
echo "Caddy was NOT edited. Insert the following inside the wheresthepelican.com"
echo "site block, BEFORE the existing handle_path /drone-mapper/* rule, then"
echo "run: caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy"
echo
cat "$INSTALL_DIR/deploy/caddy-snippet.caddy"
echo
echo "After Caddy is reloaded, check:"
echo "  curl -sS https://wheresthepelican.com/drone-mapper/api/health"
echo "Do not change Quantum Stockpile, WebODM, or any other site route."
