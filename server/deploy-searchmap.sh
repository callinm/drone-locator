#!/usr/bin/env bash
# Deploy Drone Locator search maps (find-api backend). Run on the VPS as ubuntu with sudo.
# Usage: deploy-searchmap.sh <staging dir containing searchmap.py app.py store.py>
set -euo pipefail
SRC="$1"
TS="$(TZ=America/Chicago date +%Y%m%d-%H%M%S)CST"
APP=/opt/drone-mapper-find
DATA=/var/lib/drone-mapper-find
# 0) Refuse if someone changed the live files since this branch was rebased (Nov 14 6:55 PM CST).
cd "$APP"
echo "cd93861f3320a286d83d4b24f14c8482300d06dfe2c88842cbd87bb9d5843c81  store.py
b07739ea5b27adfa6464571b12b3e6fbc071fee7e947164ac8c7db7d6d98d974  app.py
2b31005773fc7a5c8f0b43992c09b49bbf82d0d80fb79899cdee2317dc1f003b  processing.py" | sha256sum -c --quiet || { echo "live find-api changed since rebase; aborting"; exit 1; }
cd - >/dev/null
# Refuse while a Piletally/WebODM job is running.
if sudo sqlite3 -readonly "$DATA/accounts.sqlite" "select count(*) from processing_jobs where status in ('queued','uploading','processing')" | grep -qv '^0$'; then echo "processing job active; aborting"; exit 1; fi
# 1) Backups first.
sudo tar -C /opt --exclude=drone-mapper-find/.venv --exclude=__pycache__ -czf "/var/backups/drone-mapper-find-code-$TS.tgz" drone-mapper-find
sqlite3 "$DATA/locator.sqlite" ".backup /home/ubuntu/locator-$TS.sqlite"
sudo mv "/home/ubuntu/locator-$TS.sqlite" "/var/backups/locator-$TS.sqlite"
sudo cp -a /etc/systemd/system/drone-mapper-find.service.d "/var/backups/drone-mapper-find.service.d-$TS"
echo "backups: /var/backups/drone-mapper-find-code-$TS.tgz /var/backups/locator-$TS.sqlite"
# 2) Code.
for f in searchmap.py app.py store.py; do
  sudo install -m 644 -o root -g root "$SRC/$f" "$APP/$f"
done
# 3) NodeODX address (docker bridge; NodeODX has no host port). Re-check if the container is recreated.
NODE_IP="$(sudo docker inspect node-odx-1 -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')"
printf '[Service]\nEnvironment=SEARCHMAP_NODE_URL=http://%s:3000\n' "$NODE_IP" | sudo tee /etc/systemd/system/drone-mapper-find.service.d/searchmap.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart drone-mapper-find
sleep 3
systemctl is-active drone-mapper-find
curl -s -m5 http://127.0.0.1:8091/health; echo
echo "TS=$TS"
