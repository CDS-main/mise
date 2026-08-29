#!/usr/bin/env bash
# Mise — Raspberry Pi install. Run once, from the repo root, as your normal user.
#   curl -fsSL <repo>/scripts/install-pi.sh | bash    (or just: bash scripts/install-pi.sh)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$APP_DIR"

echo "==> Mise install in $APP_DIR as $USER_NAME"

sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git avahi-daemon
# avahi gives you http://mise.local — set the hostname in Raspberry Pi Imager
# or with: sudo raspi-config nonint do_hostname mise

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

# yt-dlp is optional; only needed for YouTube recipe import
./.venv/bin/pip install -q yt-dlp || echo "   (yt-dlp skipped — YouTube import will be unavailable)"

# Hardware libraries: Pi only, and only once the load cell is wired.
if grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
  echo "==> Raspberry Pi detected — installing GPIO libraries"
  ./.venv/bin/pip install -q "hx711" "RPi.GPIO" || \
    echo "   (HX711 install failed — the scale stays simulated until this works)"
fi

mkdir -p data
[ -f .env ] || { cp .env.example .env; echo "==> Created .env — put your API key in it"; }
chmod 600 .env

sed "s|/home/connor|$APP_DIR|g; s|User=connor|User=$USER_NAME|" scripts/mise.service \
  | sudo tee /etc/systemd/system/mise.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now mise

sleep 2
echo
echo "==> Done."
echo "    Local:  http://$(hostname).local:8000"
echo "    Or:     http://$(hostname -I | awk '{print $1}'):8000"
echo "    Docs:   http://$(hostname).local:8000/docs"
echo "    Logs:   journalctl -u mise -f"
echo
systemctl is-active --quiet mise && echo "    Service is running." || \
  { echo "    Service failed to start:"; journalctl -u mise -n 20 --no-pager; }
