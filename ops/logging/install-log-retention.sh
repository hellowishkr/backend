#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

for required_file in \
    10-wishkr-journald.conf \
    nginx.logrotate \
    wishkr-logs.tmpfiles; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d
install -d -o root -g root -m 0755 /etc/tmpfiles.d
install -d -o root -g adm -m 0750 /var/log/wishkr
install -d -o root -g adm -m 0750 /var/log/wishkr-trivy

install -o root -g root -m 0644 \
  "${source_dir}/10-wishkr-journald.conf" \
  /etc/systemd/journald.conf.d/10-wishkr.conf
install -o root -g root -m 0644 \
  "${source_dir}/nginx.logrotate" \
  /etc/logrotate.d/nginx
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-logs.tmpfiles" \
  /etc/tmpfiles.d/wishkr-logs.conf

for log_path in \
    /var/log/nginx/wishkr-api-access.log \
    /var/log/nginx/wishkr-api-error.log \
    /var/log/nginx/wishkr-api-metrics.log; do
  if [[ -e ${log_path} ]]; then
    chown root:adm "${log_path}"
    chmod 0640 "${log_path}"
  fi
done

# A generic Alloy package installation may opt into journal-reader groups.
# Wishkr intentionally runs metrics only and must not inherit log access.
for prohibited_group in docker adm systemd-journal; do
  if id -nG alloy 2>/dev/null | tr ' ' '\n' | grep -qx "${prohibited_group}"; then
    gpasswd --delete alloy "${prohibited_group}"
  fi
done

systemd-tmpfiles --create /etc/tmpfiles.d/wishkr-logs.conf
logrotate --debug /etc/logrotate.conf >/dev/null 2>&1
systemctl restart systemd-journald.service

if systemctl is-active --quiet alloy.service; then
  systemctl restart alloy.service
fi

echo "PASS: bounded local log-retention policy installed"
echo "Next: sudo ${source_dir}/validate-log-retention.sh"
