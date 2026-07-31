#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root" >&2
    exit 1
fi

EXPECTED_DOCKER_KEY="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

for source_file in \
    /tmp/wishkr-docker.sources \
    /tmp/wishkr-daemon.json \
    /tmp/wishkr-docker-firewall \
    /tmp/wishkr-docker-firewall.service
do
    if [ ! -f "${source_file}" ]; then
        echo "Missing staged file: ${source_file}" >&2
        exit 1
    fi
done

for conflict in \
    docker.io docker-compose docker-compose-v2 docker-doc podman-docker \
    containerd runc
do
    if dpkg-query -W -f='${db:Status-Abbrev}' "${conflict}" 2>/dev/null \
        | grep -q '^ii'
    then
        echo "Conflicting package is installed: ${conflict}" >&2
        exit 1
    fi
done

install -d -m 0755 /etc/apt/keyrings /etc/docker
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc

actual_fingerprint="$(
    gpg --batch --show-keys --with-colons /etc/apt/keyrings/docker.asc \
        | awk -F: '$1 == "fpr" { print $10; exit }'
)"
if [ "${actual_fingerprint}" != "${EXPECTED_DOCKER_KEY}" ]; then
    echo "Unexpected Docker signing-key fingerprint: ${actual_fingerprint}" >&2
    exit 1
fi

install -o root -g root -m 0644 \
    /tmp/wishkr-docker.sources /etc/apt/sources.list.d/docker.sources
install -o root -g root -m 0644 \
    /tmp/wishkr-daemon.json /etc/docker/daemon.json

python3 -m json.tool /etc/docker/daemon.json >/dev/null

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

dockerd --validate --config-file=/etc/docker/daemon.json

install -o root -g root -m 0755 \
    /tmp/wishkr-docker-firewall \
    /usr/local/sbin/wishkr-docker-firewall
install -o root -g root -m 0644 \
    /tmp/wishkr-docker-firewall.service \
    /etc/systemd/system/wishkr-docker-firewall.service

systemctl daemon-reload
systemctl enable docker.service containerd.service
systemctl restart docker.service
systemctl enable --now wishkr-docker-firewall.service
