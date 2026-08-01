#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
certificate_path=/etc/wishkr/tls/api.wishkr.com-origin.pem

for required_file in \
  export-tls-certificate-metrics.sh \
  wishkr-tls-certificate-metrics.service \
  wishkr-tls-certificate-metrics.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in openssl date sed; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: ${command_name}" >&2
    exit 1
  fi
done

if [[ ! -s ${certificate_path} ]]; then
  echo "ERROR: public origin certificate is missing" >&2
  exit 1
fi
if ! getent group alloy >/dev/null; then
  echo "ERROR: Alloy must be installed before TLS metric collection" >&2
  exit 1
fi

install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -o root -g root -m 0750 \
  "${source_dir}/export-tls-certificate-metrics.sh" \
  /usr/local/sbin/wishkr-export-tls-certificate-metrics
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-tls-certificate-metrics.service" \
  /etc/systemd/system/wishkr-tls-certificate-metrics.service
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-tls-certificate-metrics.timer" \
  /etc/systemd/system/wishkr-tls-certificate-metrics.timer

systemctl daemon-reload
systemctl enable --now wishkr-tls-certificate-metrics.timer
systemctl start wishkr-tls-certificate-metrics.service

metrics_file=/var/lib/alloy/textfile/wishkr_tls_certificate.prom
if [[ $(stat -c '%U:%G:%a' "${metrics_file}") != root:alloy:640 ]]; then
  echo "ERROR: unexpected TLS metrics file ownership or mode" >&2
  exit 1
fi

grep -qx 'wishkr_tls_certificate_collection_success{certificate="api-origin"} 1' "${metrics_file}"
grep -qx 'wishkr_tls_certificate_valid_now{certificate="api-origin"} 1' "${metrics_file}"
grep -qx 'wishkr_tls_certificate_hostname_matches{certificate="api-origin"} 1' "${metrics_file}"
seconds_until_expiry=$(awk '$1 ~ /^wishkr_tls_certificate_seconds_until_expiry/ {print $2}' "${metrics_file}")
if [[ ! ${seconds_until_expiry} =~ ^[0-9]+$ ]] || (( seconds_until_expiry <= 2592000 )); then
  echo "ERROR: origin certificate expires within 30 days" >&2
  exit 1
fi

echo "PASS: TLS certificate metrics timer installed and certificate is healthy"
