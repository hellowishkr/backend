#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root" >&2
    exit 1
fi

TLS_DIR=/etc/wishkr/tls
ORIGIN_CERT="$TLS_DIR/api.wishkr.com-origin.pem"
ORIGIN_KEY="$TLS_DIR/api.wishkr.com-origin.key"
REALIP_CONF=/etc/nginx/conf.d/cloudflare-realip.conf

test -s "$ORIGIN_CERT"
test -s "$ORIGIN_KEY"
test -s "$REALIP_CONF"

# Reject the one-day self-signed staging certificate and certificates nearing
# expiry before any public Cloudflare source network is admitted.
subject=$(openssl x509 -in "$ORIGIN_CERT" -noout -subject | sed 's/^subject=//')
issuer=$(openssl x509 -in "$ORIGIN_CERT" -noout -issuer | sed 's/^issuer=//')
if [ "$subject" = "$issuer" ]; then
    echo "Refusing to open Cloudflare ingress with the self-signed staging certificate" >&2
    exit 1
fi
openssl x509 -in "$ORIGIN_CERT" -checkend 2592000 -noout

openssl x509 -in "$ORIGIN_CERT" -pubkey -noout > "$TLS_DIR/cert.pub"
openssl pkey -in "$ORIGIN_KEY" -pubout > "$TLS_DIR/key.pub"
cmp -s "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub" || {
    rm -f "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub"
    echo "Origin certificate and private key do not match" >&2
    exit 1
}
rm -f "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub"

nginx -t

# The public Nginx listener is IPv4-only. Add exactly the IPv4 networks trusted
# by the matching Nginx real-IP snapshot; never allow port 443 from Anywhere.
sed -n 's/^set_real_ip_from \([^;]*\);/\1/p' "$REALIP_CONF" |
while IFS= read -r network; do
    case "$network" in
        *:*) continue ;;
    esac
    if ! ufw status | grep -F "$network" | grep -Fq '443/tcp'; then
        ufw allow proto tcp from "$network" to any port 443 \
            comment 'Cloudflare HTTPS origin'
    fi
done

ufw status verbose
echo "Cloudflare IPv4 HTTPS source networks enabled in UFW."
