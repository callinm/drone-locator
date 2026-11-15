#!/usr/bin/env bash
# Deploy Drone Locator search maps (find-api backend). Run on the VPS as ubuntu with sudo.
# Usage: deploy-searchmap.sh <staging dir containing searchmap.py app.py store.py>
set -euo pipefail
SRC="$1"
TS="$(TZ=America/Chicago date +%Y%m%d-%H%M%S)CST"
APP=/opt/drone-mapper-find
DATA=/var/lib/drone-mapper-find
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
