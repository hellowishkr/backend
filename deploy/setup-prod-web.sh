#!/usr/bin/env bash
# Configure the web/API server for nginx + FastAPI backend.
# Usage on web server after code is present in /opt/wishkr: bash deploy/setup-prod-web.sh [domain]
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wishkr}"
DOMAIN="${1:-api.wishkr.com}"
EMAIL="${LETSENCRYPT_EMAIL:-karan@wishkr.com}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod-web.yml}"

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "ERROR: $APP_DIR/.env is missing. Create it from .env.prod-web.example without committing secrets." >&2
  exit 1
fi

echo "[1/5] Installing nginx and certbot"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx curl

echo "[2/5] Configuring firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "[3/5] Configuring nginx reverse proxy"
CERT_PATH="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
KEY_PATH="/etc/letsencrypt/live/$DOMAIN/privkey.pem"

if [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
  cat > /etc/nginx/sites-available/wishkr-api <<EOF_NGINX
server {
    listen 80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate $CERT_PATH;
    ssl_certificate_key $KEY_PATH;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
    }
}
EOF_NGINX
else
  cat > /etc/nginx/sites-available/wishkr-api <<EOF_NGINX
server {
    listen 80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
    }
}
EOF_NGINX
fi
ln -sf /etc/nginx/sites-available/wishkr-api /etc/nginx/sites-enabled/wishkr-api
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "[4/5] Building and starting backend"
docker compose -f "$COMPOSE_FILE" up -d --build backend
docker compose -f "$COMPOSE_FILE" ps

echo "[5/5] Verifying local health"
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
    echo "Backend health check passed"
    break
  fi
  sleep 2
  if [ "$i" -eq 30 ]; then
    echo "ERROR: backend health check failed" >&2
    docker compose -f "$COMPOSE_FILE" logs --tail=100 backend >&2
    exit 1
  fi
done

if [ -f "$CERT_PATH" ] && [ -f "$KEY_PATH" ]; then
  echo "SSL certificate found for $DOMAIN; nginx is listening on HTTPS."
else
  cat <<EOF_SSL
SSL is not requested automatically by this script because DNS cutover is a separate controlled step.
After api.wishkr.com points to this server, run:
  certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m $EMAIL --redirect
EOF_SSL
fi
