#!/usr/bin/env bash
# Configure worker/scheduler server. This script never runs migrations.
# Usage on worker server after code is present in /opt/wishkr: bash deploy/setup-prod-worker.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wishkr}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod-worker.yml}"

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/.env is missing. Create it from .env.prod-worker.example without committing secrets." >&2
  exit 1
fi

echo "[1/4] Configuring firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "[2/4] Building and starting scheduler"
docker compose -f "$COMPOSE_FILE" up -d --build scheduler

echo "[3/4] Verifying scheduler container and DB connectivity"
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" exec -T scheduler python - <<'PY'
import asyncio
from sqlalchemy import text
from app.db.session import engine

async def main():
    async with engine.connect() as conn:
        value = await conn.scalar(text("select 1"))
        assert value == 1
    await engine.dispose()

asyncio.run(main())
print("DB connectivity OK; no migrations were run")
PY

echo "[4/4] Checking logs for startup"
docker compose -f "$COMPOSE_FILE" logs --tail=80 scheduler

echo "Worker setup complete. This server does not run alembic migrations."
