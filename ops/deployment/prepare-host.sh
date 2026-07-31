#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

install -d -o root -g root -m 0755 /opt/wishkr
install -d -o root -g root -m 0755 /opt/wishkr/releases
install -d -o root -g root -m 0700 /etc/wishkr
install -d -o root -g root -m 0700 /etc/wishkr/secrets
install -d -o root -g root -m 0700 /var/lib/wishkr
install -d -o root -g root -m 0700 /var/backups/wishkr
install -d -o root -g adm -m 0750 /var/log/wishkr

for network in wishkr-app wishkr-data; do
  if ! docker network inspect "${network}" >/dev/null 2>&1; then
    echo "ERROR: required Docker network is missing: ${network}" >&2
    exit 1
  fi
done

if [[ -e /opt/wishkr/current && ! -L /opt/wishkr/current ]]; then
  echo "ERROR: /opt/wishkr/current exists but is not a symlink" >&2
  exit 1
fi

echo "PASS: Wishkr production host directories and Docker networks are ready"
