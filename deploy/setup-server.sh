#!/bin/bash
# Wishkr Server Setup — Run once on a fresh Ubuntu 24.04 Hetzner VPS
# Usage: ssh root@YOUR_IP 'bash -s' < deploy/setup-server.sh
set -euo pipefail

DOMAIN="${1:-api.wishkr.com}"
EMAIL="karan@wishkr.com"

echo "=== Wishkr Server Setup ==="
echo "Domain: $DOMAIN"
echo ""

# 1. System updates
echo "[1/8] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq ufw fail2ban curl git unattended-upgrades

# 2. Firewall
echo "[2/8] Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (for Let's Encrypt)
ufw allow 443/tcp   # HTTPS
ufw --force enable
echo "Firewall: only 22, 80, 443 open"

# 3. SSH hardening
echo "[3/8] Hardening SSH..."
sed -i 's/#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh
echo "SSH: key-only auth, no passwords"

# 4. fail2ban
echo "[4/8] Setting up fail2ban..."
cat > /etc/fail2ban/jail.local << 'JAIL'
[sshd]
enabled = true
port = ssh
maxretry = 5
bantime = 3600
findtime = 600
JAIL
systemctl enable fail2ban
systemctl restart fail2ban
echo "fail2ban: 5 failed attempts = 1 hour ban"

# 5. Auto security updates
echo "[5/8] Enabling automatic security updates..."
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'AUTOUP'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
AUTOUP
echo "Security updates: auto-installed daily"

# 6. Install Docker
echo "[6/8] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
fi
echo "Docker: $(docker --version)"

# 7. Install Nginx + Certbot
echo "[7/8] Installing Nginx + Certbot..."
apt-get install -y -qq nginx certbot python3-certbot-nginx

# Nginx config for Wishkr API
cat > /etc/nginx/sites-available/wishkr << NGINX
server {
    listen 80;
    server_name $DOMAIN;

    # Let's Encrypt challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts for AI agent calls
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/wishkr /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
echo "Nginx: reverse proxy configured for $DOMAIN"

# 8. Create app directory
echo "[8/8] Setting up app directory..."
mkdir -p /opt/wishkr
mkdir -p /opt/wishkr/backups

# Daily DB backup cron
cat > /etc/cron.daily/wishkr-backup << 'BACKUP'
#!/bin/bash
# Daily PostgreSQL backup
BACKUP_DIR="/opt/wishkr/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
docker exec wishkr-backend-db-1 pg_dump -U wishkr wishkr | gzip > "$BACKUP_DIR/wishkr_$TIMESTAMP.sql.gz"
# Keep only last 7 days
find "$BACKUP_DIR" -name "wishkr_*.sql.gz" -mtime +7 -delete
BACKUP
chmod +x /etc/cron.daily/wishkr-backup
echo "DB backup: daily, 7-day retention"

echo ""
echo "=== Server setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Point DNS: $DOMAIN -> $(curl -s ifconfig.me)"
echo "  2. Run: certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m $EMAIL"
echo "  3. Clone repo to /opt/wishkr and deploy"
echo ""
echo "Server IP: $(curl -s ifconfig.me)"
