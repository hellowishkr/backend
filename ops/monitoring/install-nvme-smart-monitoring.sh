#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

for required_file in \
  export-nvme-smart-metrics.sh \
  wishkr-nvme-smart-metrics.service \
  wishkr-nvme-smart-metrics.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in smartctl jq; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: ${command_name}" >&2
    exit 1
  fi
done

if ! getent group alloy >/dev/null; then
  echo "ERROR: Alloy must be installed before NVMe metric collection" >&2
  exit 1
fi

install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -o root -g root -m 0750 \
  "${source_dir}/export-nvme-smart-metrics.sh" \
  /usr/local/sbin/wishkr-export-nvme-smart-metrics
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-nvme-smart-metrics.service" \
  /etc/systemd/system/wishkr-nvme-smart-metrics.service
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-nvme-smart-metrics.timer" \
  /etc/systemd/system/wishkr-nvme-smart-metrics.timer

systemctl daemon-reload
systemctl enable --now wishkr-nvme-smart-metrics.timer
systemctl start wishkr-nvme-smart-metrics.service

metrics_file=/var/lib/alloy/textfile/wishkr_nvme_smart.prom
if [[ $(stat -c '%U:%G:%a' "${metrics_file}") != root:alloy:640 ]]; then
  echo "ERROR: unexpected NVMe metrics file ownership or mode" >&2
  exit 1
fi

grep -Eq '^wishkr_nvme_smart_collection_success\{device="nvme0n1"\} 1$' "${metrics_file}"
grep -Eq '^wishkr_nvme_smart_collection_success\{device="nvme1n1"\} 1$' "${metrics_file}"

echo "PASS: NVMe SMART metrics timer installed and both devices collected"
