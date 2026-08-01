#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

for required_file in \
  export-container-metrics.sh \
  wishkr-container-metrics.service \
  wishkr-container-metrics.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in docker jq; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: ${command_name}" >&2
    exit 1
  fi
done

if ! getent group alloy >/dev/null; then
  echo "ERROR: Alloy must be installed before container metric collection" >&2
  exit 1
fi

install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -o root -g root -m 0750 \
  "${source_dir}/export-container-metrics.sh" \
  /usr/local/sbin/wishkr-export-container-metrics
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-container-metrics.service" \
  /etc/systemd/system/wishkr-container-metrics.service
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-container-metrics.timer" \
  /etc/systemd/system/wishkr-container-metrics.timer

systemctl daemon-reload
systemctl enable --now wishkr-container-metrics.timer
systemctl start wishkr-container-metrics.service

metrics_file=/var/lib/alloy/textfile/wishkr_containers.prom
if [[ $(stat -c '%U:%G:%a' "${metrics_file}") != root:alloy:640 ]]; then
  echo "ERROR: unexpected container metrics file ownership or mode" >&2
  exit 1
fi

for container in wishkr-db-1 wishkr-redis-1 wishkr-backend-1 wishkr-scheduler-1; do
  grep -Eq "^wishkr_container_present\\{container=\"${container}\"\\} 1$" "${metrics_file}"
  grep -Eq "^wishkr_container_running\\{container=\"${container}\"\\} 1$" "${metrics_file}"
  grep -Eq "^wishkr_container_healthy\\{container=\"${container}\"\\} 1$" "${metrics_file}"
done

echo "PASS: container metrics timer installed and all expected containers are healthy"
