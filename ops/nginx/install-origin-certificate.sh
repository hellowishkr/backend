#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root" >&2
    exit 1
fi

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 /path/to/cloudflare-origin-certificate.pem" >&2
    exit 2
fi

SOURCE_CERT=$1
TLS_DIR=/etc/wishkr/tls
ORIGIN_KEY="$TLS_DIR/api.wishkr.com-origin.key"
ORIGIN_CERT="$TLS_DIR/api.wishkr.com-origin.pem"

test -s "$SOURCE_CERT"
test -s "$ORIGIN_KEY"

openssl x509 -in "$SOURCE_CERT" -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in "$SOURCE_CERT" -checkhost api.wishkr.com -noout
openssl x509 -in "$SOURCE_CERT" -checkend 2592000 -noout
openssl x509 -in "$SOURCE_CERT" -pubkey -noout > "$TLS_DIR/cert.pub"
openssl pkey -in "$ORIGIN_KEY" -pubout > "$TLS_DIR/key.pub"
cmp -s "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub" || {
    rm -f "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub"
    echo "Certificate does not match the OVH private key" >&2
    exit 1
}
rm -f "$TLS_DIR/cert.pub" "$TLS_DIR/key.pub"

install -o root -g root -m 0644 "$SOURCE_CERT" "$ORIGIN_CERT.new"
mv -f "$ORIGIN_CERT.new" "$ORIGIN_CERT"
nginx -t
systemctl reload nginx

echo "Cloudflare Origin CA certificate installed and Nginx reloaded."
