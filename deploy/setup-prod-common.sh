#!/usr/bin/env bash
# Common one-time setup for Wishkr production servers.
# Usage: ssh root@SERVER_IP 'bash -s' < deploy/setup-prod-common.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wishkr}"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

echo "[1/6] Updating packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

echo "[2/6] Installing base packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates \
  curl \
  fail2ban \
  git \
  gnupg \
  lsb-release \
  rsync \
  ufw \
  unattended-upgrades

echo "[3/6] Installing Docker if missing"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable docker
systemctl start docker

echo "[4/6] Hardening SSH for key-only auth"
install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-wishkr-hardening.conf <<'EOF_SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
EOF_SSH
sshd -t
systemctl reload ssh || systemctl reload sshd

echo "[5/6] Enabling fail2ban and unattended upgrades"
cat > /etc/fail2ban/jail.local <<'EOF_JAIL'
[sshd]
enabled = true
port = ssh
maxretry = 5
bantime = 3600
findtime = 600
EOF_JAIL
systemctl enable fail2ban
systemctl restart fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF_AUTO'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF_AUTO

echo "[6/6] Creating app directory"
mkdir -p "$APP_DIR"
chmod 755 "$APP_DIR"

echo "Common setup complete. Role-specific setup is still required."
