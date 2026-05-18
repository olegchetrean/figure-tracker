#!/bin/bash
# Deploy from local Mac to infograph server
set -e

SERVER="infograph"
REMOTE="/opt/figure-tracker"

echo "=== Deploying to $SERVER ==="

# Copy all files
rsync -avz --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  ./backend/   $SERVER:$REMOTE/backend/
rsync -avz \
  ./frontend/  $SERVER:$REMOTE/frontend/
rsync -avz \
  ./infra/     $SERVER:$REMOTE/infra/
rsync -avz \
  ./requirements.txt $SERVER:$REMOTE/backend/

# Copy .env if it doesn't exist on server yet
ssh $SERVER "test -f $REMOTE/.env || echo 'WARNING: create $REMOTE/.env from .env.example'"

# If first deploy — run setup
if ssh $SERVER "test ! -d $REMOTE/venv"; then
  echo "First deploy — running setup_server.sh"
  ssh $SERVER "bash $REMOTE/infra/setup_server.sh"
else
  # Subsequent deploy — just reinstall deps & restart
  ssh $SERVER "$REMOTE/venv/bin/pip install -r $REMOTE/backend/requirements.txt -q"
  ssh $SERVER "sudo systemctl restart figure-tracker-api"
  echo "Service restarted."
fi

echo ""
echo "=== Deploy complete ==="
echo "Site: http://57.128.108.199"
echo "Logs: ssh $SERVER 'journalctl -u figure-tracker-api -f'"
