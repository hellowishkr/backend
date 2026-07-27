#!/usr/bin/env bash
# Deploy web/API from local machine using rsync. Does not rely on remote Git.
# Usage: deploy/deploy-prod-web.sh [web_ip]
set -euo pipefail

SERVER_IP="${1:-5.223.77.195}"
REMOTE_DIR="${REMOTE_DIR:-/opt/wishkr}"
COMPOSE_FILE="docker-compose.prod-web.yml"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"

rsync_common=(
  -az --delete
  --exclude .env
  --exclude .git
  --exclude .venv
  --exclude __pycache__
  --exclude .mypy_cache
  --exclude .pytest_cache
  --exclude .ruff_cache
  --exclude graphify-out
  --exclude backups
  --exclude '*.pyc'
  -e "ssh -o StrictHostKeyChecking=accept-new"
)

echo "[1/5] Syncing code to web server $SERVER_IP"
rsync "${rsync_common[@]}" ./ "root@$SERVER_IP:$REMOTE_DIR/"

echo "[2/5] Checking remote .env"
ssh root@"$SERVER_IP" "test -f '$REMOTE_DIR/.env' || { echo 'ERROR: $REMOTE_DIR/.env missing'; exit 1; }"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
echo "[3/5] Building backend image"
ssh root@"$SERVER_IP" "cd '$REMOTE_DIR' && GIT_SHA='$GIT_SHA' docker compose -f '$COMPOSE_FILE' build backend"

echo "[4/5] Running migration through web compose, then starting backend"
ssh root@"$SERVER_IP" "cd '$REMOTE_DIR' && GIT_SHA='$GIT_SHA' docker compose -f '$COMPOSE_FILE' up -d backend"

echo "[5/5] Health check"
ssh root@"$SERVER_IP" "for i in \$(seq 1 30); do curl -fsS '$HEALTH_URL' >/dev/null && exit 0; sleep 2; done; docker compose -f '$REMOTE_DIR/$COMPOSE_FILE' logs --tail=120 backend; exit 1"

echo "Web deploy complete."
