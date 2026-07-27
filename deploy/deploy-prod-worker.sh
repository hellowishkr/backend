#!/usr/bin/env bash
# Deploy worker/scheduler from local machine using rsync. Never runs migrations.
# Usage: deploy/deploy-prod-worker.sh [worker_ip]
set -euo pipefail

SERVER_IP="${1:-5.223.79.238}"
REMOTE_DIR="${REMOTE_DIR:-/opt/wishkr}"
COMPOSE_FILE="docker-compose.prod-worker.yml"

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

echo "[1/4] Syncing code to worker server $SERVER_IP"
rsync "${rsync_common[@]}" ./ "root@$SERVER_IP:$REMOTE_DIR/"

echo "[2/4] Checking remote .env"
ssh root@"$SERVER_IP" "test -f '$REMOTE_DIR/.env' || { echo 'ERROR: $REMOTE_DIR/.env missing'; exit 1; }"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
echo "[3/4] Building scheduler image"
ssh root@"$SERVER_IP" "cd '$REMOTE_DIR' && GIT_SHA='$GIT_SHA' docker compose -f '$COMPOSE_FILE' build scheduler"

echo "[4/4] Starting scheduler without migrations"
ssh root@"$SERVER_IP" "cd '$REMOTE_DIR' && GIT_SHA='$GIT_SHA' docker compose -f '$COMPOSE_FILE' up -d scheduler && docker compose -f '$COMPOSE_FILE' ps"

echo "Worker deploy complete. No alembic migration was run."
