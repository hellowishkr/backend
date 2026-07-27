#!/usr/bin/env bash
# Configure the existing DB server for Postgres + Redis only.
# Usage on DB server after code is present in /opt/wishkr: bash deploy/setup-prod-db.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wishkr}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod-db.yml}"
WEB_PRIVATE_IP="${WEB_PRIVATE_IP:?Set WEB_PRIVATE_IP to the web server Hetzner private-network or WireGuard IP}"
WORKER_PRIVATE_IP="${WORKER_PRIVATE_IP:?Set WORKER_PRIVATE_IP to the worker server Hetzner private-network or WireGuard IP}"
DB_BIND_IP="${DB_BIND_IP:-}"
BACKUP_SCRIPT="/usr/local/bin/wishkr-backup-prod-db"
CRON_FILE="/etc/cron.d/wishkr-prod-db-backup"

cd "$APP_DIR"

echo "[1/6] Safety checks"
if ! docker volume inspect wishkr_pgdata >/dev/null 2>&1; then
  echo "ERROR: required existing Docker volume wishkr_pgdata was not found. Refusing to continue." >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/.env is missing. Create it from .env.prod-db.example without committing secrets." >&2
  exit 1
fi
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "ERROR: $APP_DIR/$COMPOSE_FILE is missing." >&2
  exit 1
fi
if [ -z "$DB_BIND_IP" ] && [ -f .env ]; then
  DB_BIND_IP="$(grep -E '^DB_BIND_IP=' .env | tail -n1 | cut -d= -f2- || true)"
fi
if [ -z "$DB_BIND_IP" ]; then
  echo "ERROR: DB_BIND_IP must be set to the DB server private-network or WireGuard IP." >&2
  exit 1
fi
for ip_name in DB_BIND_IP WEB_PRIVATE_IP WORKER_PRIVATE_IP; do
  ip_value="${!ip_name}"
  case "$ip_value" in
    10.*|172.16.*|172.17.*|172.18.*|172.19.*|172.20.*|172.21.*|172.22.*|172.23.*|172.24.*|172.25.*|172.26.*|172.27.*|172.28.*|172.29.*|172.30.*|172.31.*|192.168.*)
      ;;
    *)
      echo "ERROR: $ip_name=$ip_value does not look like an RFC1918 private IP. Refusing to expose DB/Redis on a public address." >&2
      exit 1
      ;;
  esac
done

echo "[2/6] Configuring firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow from "$WEB_PRIVATE_IP" to any port 5432 proto tcp
ufw allow from "$WORKER_PRIVATE_IP" to any port 5432 proto tcp
ufw allow from "$WEB_PRIVATE_IP" to any port 6379 proto tcp
ufw allow from "$WORKER_PRIVATE_IP" to any port 6379 proto tcp
ufw --force enable
ufw status verbose

echo "[3/6] Installing backup script"
install -m 0755 deploy/backup-prod-db.sh "$BACKUP_SCRIPT"
cat > "$CRON_FILE" <<EOF_CRON
# Wishkr production DB backup, daily at 02:15 server time.
15 2 * * * root APP_DIR=$APP_DIR $BACKUP_SCRIPT >> /var/log/wishkr-backup.log 2>&1
EOF_CRON
chmod 0644 "$CRON_FILE"

echo "[4/6] Starting DB/Redis compose without recreating existing Postgres volume"
docker compose -f "$COMPOSE_FILE" up -d db redis

echo "[5/6] Verifying health"
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U "${POSTGRES_USER:-wishkr}" -d "${POSTGRES_DB:-wishkr}"
docker compose -f "$COMPOSE_FILE" exec -T redis redis-cli ping

echo "[6/6] Confirming public exposure rules"
if ufw status numbered | grep -E '5432|6379' | grep -E 'Anywhere|0\.0\.0\.0/0'; then
  echo "ERROR: unsafe public DB/Redis firewall rule detected." >&2
  exit 1
fi

echo "DB setup complete. Do not stop the old backend until cutover is confirmed."
