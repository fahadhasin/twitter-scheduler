#!/usr/bin/env bash
set -e

# ── Configure these for your setup ──────────────────────────────────────────
PI_USER="your-pi-username"
PI_HOST="your-pi-hostname.local"
REMOTE_DIR="/home/${PI_USER}/twitter-scheduler"
SERVICE="twitter-scheduler"
# ────────────────────────────────────────────────────────────────────────────

PI="${PI_USER}@${PI_HOST}"

echo "==> Syncing files to Pi..."
rsync -av \
  --exclude venv \
  --exclude .env \
  --exclude data \
  --exclude __pycache__ \
  --exclude "*.pyc" \
  . "${PI}:${REMOTE_DIR}/"

echo "==> Setting up venv and installing dependencies..."
ssh "$PI" bash << ENDSSH
set -e
cd ${REMOTE_DIR}
mkdir -p data/images

if [ ! -d venv ]; then
  python3 -m venv venv
fi

source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ENDSSH

echo "==> Installing systemd service..."
ssh "$PI" bash << ENDSSH
set -e
sudo cp ${REMOTE_DIR}/twitter-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable twitter-scheduler.service
ENDSSH

echo "==> Checking for .env..."
if ! ssh "$PI" test -f "${REMOTE_DIR}/.env"; then
  echo ""
  echo "  *** .env NOT FOUND on Pi ***"
  echo "  Copy your credentials:"
  echo "    scp .env ${PI}:${REMOTE_DIR}/.env"
  echo "  Then start the service:"
  echo "    ssh ${PI} 'sudo systemctl start twitter-scheduler.service'"
  echo ""
else
  echo "==> Restarting service..."
  ssh "$PI" sudo systemctl restart twitter-scheduler.service
  echo "==> Done. Checking status..."
  ssh "$PI" sudo systemctl status twitter-scheduler.service --no-pager
fi
