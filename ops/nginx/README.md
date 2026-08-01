# Wishkr OVH Nginx and Cloudflare Origin

This directory contains the public-edge configuration for `api.wishkr.com`.
It never contains private keys, signed production certificates, tokens, or
Cloudflare account credentials.

## Security model

- Nginx is the only public application listener and proxies to
  `127.0.0.1:8000`.
- Only TLS 1.2 and TLS 1.3 are enabled.
- Unknown TLS server names are rejected during the handshake.
- Public requests require Cloudflare Authenticated Origin Pull verification.
- Loopback and `10.77.0.0/24` may bypass mTLS strictly for VPN administration.
- UFW admits port 443 only from Cloudflare's official IPv4 HTTP proxy ranges
  and the WireGuard interface.
- `CF-Connecting-IP` is trusted only when the TCP peer is in an official
  Cloudflare range.
- Access logs record `$uri`, never `$request_uri`, so query parameters such as
  Meta's webhook verification token are not retained.
- Wishkr access, error, and monitoring logs are `root:adm 0640` from first
  creation and rotate daily for 14 days under the Item 12 retention policy.
- Monitoring uses a separate log containing only numeric timestamp and response
  status plus an aggregate `stub_status` endpoint bound to `127.0.0.1:8081`.
  The monitoring exporter cannot read the normal access or error logs.
- `/internal/` is VPN-only. Webhooks retain application HMAC validation and are
  exempt from the low-volume login limiter.

The snapshot in `cloudflare-realip.conf` came from Cloudflare's official IPv4
and IPv6 lists on 2026-07-31. Review it for drift before firewall changes.

## Staged installation

Run `install-nginx.sh` as root. It installs Nginx, creates an ECDSA P-256
private key directly in `/etc/wishkr/tls`, creates a CSR, installs a one-day
self-signed placeholder, and admits HTTPS only over WireGuard.

The private key must never leave the OVH host. Copy only the public CSR to the
local clipboard:

```bash
ssh wishkr-ovh \
  'sudo cat /etc/wishkr/tls/api.wishkr.com-origin.csr' | pbcopy
```

In Cloudflare, select the `wishkr.com` zone, then:

1. Go to **SSL/TLS > Origin Server > Origin Certificates**.
2. Select **Create Certificate**.
3. Select **Use my private key and CSR** and paste the clipboard contents.
4. Confirm the hostname is exactly `api.wishkr.com`.
5. Select a three-year validity period, or the nearest shorter available
   production period, and create the certificate.
6. Copy the signed Origin Certificate in PEM format. Do not generate or copy a
   private key in Cloudflare; the private key already exists only on OVH.

Stream the signed public certificate directly to OVH and install it:

```bash
pbpaste | ssh wishkr-ovh \
  'sudo tee /tmp/api.wishkr.com-origin.pem >/dev/null'
ssh wishkr-ovh \
  'sudo /tmp/wishkr-nginx-item9/install-origin-certificate.sh \
  /tmp/api.wishkr.com-origin.pem && \
  sudo rm -f /tmp/api.wishkr.com-origin.pem'
```

The installer verifies the certificate's dates, SAN, and public-key match
before atomically replacing the placeholder and reloading Nginx.

## Cloudflare controls before public ingress

Do not alter the production `api.wishkr.com` DNS record in this item.

1. Confirm Cloudflare SSL/TLS mode is **Full (strict)** for the API hostname.
   Do not make a zone-wide change until every other proxied origin in the zone
   is known to have a compatible certificate.
2. Go to **SSL/TLS > Origin Server > Authenticated Origin Pulls** and enable
   Global Authenticated Origin Pulls. The origin uses Cloudflare's published
   global AOP CA plus an explicit request gate.
3. Run `enable-cloudflare-origin-firewall.sh` as root to add the matching
   Cloudflare IPv4 ranges to UFW.
4. In the OVH Edge Network Firewall, allow TCP destination port 443 from each
   Cloudflare IPv4 range before the final IPv4 deny rule. Keep ports 80, 22,
   8000, 5432, and 6379 closed publicly.
5. Add a Cloudflare WAF Skip rule scoped only to
   `http.host eq "api.wishkr.com" and http.request.uri.path eq
   "/webhooks/whatsapp"`. Skip browser challenges/Bot Fight features that
   cannot process Meta server-to-server webhooks; do not bypass application
   HMAC validation.

Production DNS remains on Hetzner until the staged database, scheduler,
webhook, backup, and cutover checks are approved.
