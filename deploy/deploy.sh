#!/bin/bash
# Wishkr Deploy — Run from your local machine to deploy/update
# Usage: ./deploy/deploy.sh YOUR_SERVER_IP [DOMAIN]
set -euo pipefail

SERVER_IP="${1:?Usage: ./deploy/deploy.sh SERVER_IP [DOMAIN]}"
DOMAIN="${2:-api.wishkr.com}"
REMOTE_DIR="/opt/wishkr"

echo "=== Deploying Wishkr to $SERVER_IP ==="

# 1. Sync code to server (exclude sensitive files)
echo "[1/4] Syncing code..."
rsync -avz --delete \
    --exclude '.env' \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude 'graphify-out' \
    --exclude '.DS_Store' \
    --exclude 'deploy/' \
    -e "ssh -o StrictHostKeyChecking=accept-new" \
    ./ root@"$SERVER_IP":"$REMOTE_DIR"/

echo "[2/4] Checking .env exists on server..."
ssh root@"$SERVER_IP" "
    if [ ! -f $REMOTE_DIR/.env ]; then
        echo 'ERROR: .env not found on server!'
        echo 'Create it first: ssh root@$SERVER_IP \"nano $REMOTE_DIR/.env\"'
        echo 'Use .env.example as template'
        exit 1
    fi
"

echo "[3/4] Building and starting containers..."
# Capture the local git SHA so Sentry can tag every event with the
# release. Not a secret. Falls back to empty when not a git checkout —
# deploys still work, Sentry just can't group by release.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '')"
echo "    release tag: ${GIT_SHA:-<unset>}"
ssh root@"$SERVER_IP" "
    cd $REMOTE_DIR
    export GIT_SHA='$GIT_SHA'
    docker compose down
    docker compose up -d --build
    echo 'Waiting for containers to start...'
    sleep 5
    docker compose ps
"

echo "[4/4] Health check..."
sleep 3
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://$SERVER_IP:8000/" 2>/dev/null || echo "000")
if [ "$HTTP_STATUS" = "000" ]; then
    echo "Direct health check failed (might be behind nginx). Checking docker..."
    ssh root@"$SERVER_IP" "docker compose -f $REMOTE_DIR/docker-compose.yml logs --tail=20 backend"
else
    echo "Backend responding: HTTP $HTTP_STATUS"
fi

echo ""
echo "=== Deploy complete ==="
echo "API: https://$DOMAIN (once DNS + SSL are configured)"
echo "Logs: ssh root@$SERVER_IP 'cd $REMOTE_DIR && docker compose logs -f'"
