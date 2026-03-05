#!/usr/bin/env bash
# Deploy twitter-scheduler to a remote Linux server with systemd.
# Requires: rsync, ssh, a Python 3.11+ server with systemd.
#
# Setup:
#   1. Copy .env.example to .env and fill in your credentials
#   2. Edit the three variables below
#   3. Run: ./deploy.sh
set -e

REMOTE_USER="your-username"          # SSH user on the remote server
REMOTE_HOST="your-server-host"       # hostname or IP of the remote server
REMOTE_DIR="/home/${REMOTE_USER}/twitter-scheduler"

# ── Deploy ───────────────────────────────────────────────────────────────────
PI="${REMOTE_USER}@${REMOTE_HOST}"

echo "==> Syncing files to ${REMOTE_HOST}..."
rsync -av \
  --exclude venv \
  --exclude .env \
  --exclude data \
  --exclude __pycache__ \
  --exclude "*.pyc" \
  . "${PI}:${REMOTE_DIR}/"

echo "==> Installing dependencies..."
ssh "$PI" bash << ENDSSH
set -e
cd "${REMOTE_DIR}"
mkdir -p data/images
[ ! -d venv ] && python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
playwright install chromium --with-deps
ENDSSH

echo "==> Installing systemd service..."
ssh "$PI" bash << ENDSSH
set -e
# Substitute the real username into the service file before installing
sed "s|<your-username>|${REMOTE_USER}|g" "${REMOTE_DIR}/twitter-scheduler.service" \
  | sudo tee /etc/systemd/system/twitter-scheduler.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable twitter-scheduler.service
ENDSSH

echo "==> Checking for .env..."
if ! ssh "$PI" test -f "${REMOTE_DIR}/.env"; then
  echo ""
  echo "  *** .env NOT FOUND on server ***"
  echo "  Copy your credentials file:"
  echo "    scp .env ${PI}:${REMOTE_DIR}/.env"
  echo "  Then start the service:"
  echo "    ssh ${PI} 'sudo systemctl start twitter-scheduler.service'"
  echo ""
else
  echo "==> Restarting service..."
  ssh "$PI" sudo systemctl restart twitter-scheduler.service
  ssh "$PI" sudo systemctl status twitter-scheduler.service --no-pager
fi
