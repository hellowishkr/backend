#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TLS_DIR=/etc/wishkr/tls
ORIGIN_KEY="$TLS_DIR/api.wishkr.com-origin.key"
ORIGIN_CSR="$TLS_DIR/api.wishkr.com-origin.csr"
ORIGIN_CERT="$TLS_DIR/api.wishkr.com-origin.pem"
AOP_CA="$TLS_DIR/cloudflare-origin-pull-ca.pem"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends nginx curl openssl ca-certificates

install -d -o root -g root -m 0700 "$TLS_DIR"
install -d -o root -g root -m 0755 /etc/nginx/snippets

if [ ! -s "$ORIGIN_KEY" ]; then
    openssl genpkey \
        -algorithm EC \
        -pkeyopt ec_paramgen_curve:P-256 \
        -out "$ORIGIN_KEY"
fi
chown root:root "$ORIGIN_KEY"
chmod 0600 "$ORIGIN_KEY"

openssl req -new -sha256 \
    -key "$ORIGIN_KEY" \
    -subj "/CN=api.wishkr.com/O=Wishkr" \
    -addext "subjectAltName=DNS:api.wishkr.com" \
    -out "$ORIGIN_CSR"
chown root:root "$ORIGIN_CSR"
chmod 0644 "$ORIGIN_CSR"

# A one-day placeholder permits VPN-only config validation. Cloudflare Full
# (strict) must not be enabled until this file is replaced by the signed Origin
# CA certificate generated from the CSR above.
if [ ! -s "$ORIGIN_CERT" ]; then
    openssl x509 -req -sha256 -days 1 \
        -in "$ORIGIN_CSR" \
        -signkey "$ORIGIN_KEY" \
        -copy_extensions copy \
        -out "$ORIGIN_CERT"
fi
chown root:root "$ORIGIN_CERT"
chmod 0644 "$ORIGIN_CERT"

curl --fail --silent --show-error --location \
    https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem \
    -o "$AOP_CA.download"
openssl x509 -in "$AOP_CA.download" -noout -subject -issuer -dates
install -o root -g root -m 0644 "$AOP_CA.download" "$AOP_CA"
rm -f "$AOP_CA.download"

install -o root -g root -m 0644 \
    "$SCRIPT_DIR/00-wishkr-hardening.conf" \
    /etc/nginx/conf.d/00-wishkr-hardening.conf
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/cloudflare-realip.conf" \
    /etc/nginx/conf.d/cloudflare-realip.conf
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/wishkr-status.conf" \
    /etc/nginx/conf.d/wishkr-status.conf
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/wishkr-proxy.conf" \
    /etc/nginx/snippets/wishkr-proxy.conf
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/wishkr-api.conf" \
    /etc/nginx/sites-available/wishkr-api

ln -sfn /etc/nginx/sites-available/wishkr-api /etc/nginx/sites-enabled/wishkr-api
rm -f /etc/nginx/sites-enabled/default

# Nginx otherwise creates new per-site logs under its root master process with
# the host umask, which can leave them world-readable until the first rotation.
# Preserve existing contents while enforcing the intended operator-only mode.
for log_path in \
    /var/log/nginx/wishkr-api-access.log \
    /var/log/nginx/wishkr-api-error.log \
    /var/log/nginx/wishkr-api-metrics.log; do
  if [ ! -e "$log_path" ]; then
    install -o root -g adm -m 0640 /dev/null "$log_path"
  else
    chown root:adm "$log_path"
    chmod 0640 "$log_path"
  fi
done

nginx -t
systemctl enable --now nginx
systemctl reload nginx

# Staged administration access only. Cloudflare source networks are added
# after the signed Origin CA certificate and AOP dashboard toggle are ready.
ufw allow in on wg-admin to any port 443 proto tcp comment 'Nginx TLS over WireGuard'

echo "Nginx staged successfully."
echo "Cloudflare Origin CA CSR: $ORIGIN_CSR"
echo "Replace placeholder certificate at: $ORIGIN_CERT"
