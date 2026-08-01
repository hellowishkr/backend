#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
nginx_source_dir=${2:-$(cd "${source_dir}/../nginx" && pwd)}

for required_file in \
  export-nginx-metrics.sh \
  wishkr-nginx-metrics.service \
  wishkr-nginx-metrics.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done
for required_file in 00-wishkr-hardening.conf wishkr-api.conf wishkr-status.conf; do
  if [[ ! -f ${nginx_source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${nginx_source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in nginx curl awk grep ss; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: ${command_name}" >&2
    exit 1
  fi
done
if ! nginx -V 2>&1 | grep -q -- '--with-http_stub_status_module'; then
  echo "ERROR: Nginx was built without the stub_status module" >&2
  exit 1
fi
if ! getent group alloy >/dev/null; then
  echo "ERROR: Alloy must be installed before Nginx metric collection" >&2
  exit 1
fi

install -o root -g root -m 0644 \
  "${nginx_source_dir}/00-wishkr-hardening.conf" \
  /etc/nginx/conf.d/00-wishkr-hardening.conf
install -o root -g root -m 0644 \
  "${nginx_source_dir}/wishkr-status.conf" \
  /etc/nginx/conf.d/wishkr-status.conf
install -o root -g root -m 0644 \
  "${nginx_source_dir}/wishkr-api.conf" \
  /etc/nginx/sites-available/wishkr-api

nginx -t
systemctl reload nginx.service

listener_ready=0
for _ in {1..10}; do
  if ss -lntH 'sport = :8081' | awk '{print $4}' | grep -qx '127.0.0.1:8081'; then
    listener_ready=1
    break
  fi
  sleep 1
done
if (( listener_ready != 1 )); then
  echo "ERROR: Nginx status listener is not bound exclusively to IPv4 loopback" >&2
  exit 1
fi
if ss -lntH 'sport = :8081' | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\[::\]|\*):8081$'; then
  echo "ERROR: Nginx status listener has a public or wildcard bind" >&2
  exit 1
fi
curl --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8081/nginx_status >/dev/null

# Create one privacy-safe numeric metrics-log record before the first exporter
# run. Loopback is an explicitly permitted administrative origin.
curl --fail --silent --show-error --insecure --max-time 10 \
  --resolve api.wishkr.com:443:127.0.0.1 \
  https://api.wishkr.com/health >/dev/null

install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -o root -g root -m 0750 \
  "${source_dir}/export-nginx-metrics.sh" \
  /usr/local/sbin/wishkr-export-nginx-metrics
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-nginx-metrics.service" \
  /etc/systemd/system/wishkr-nginx-metrics.service
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-nginx-metrics.timer" \
  /etc/systemd/system/wishkr-nginx-metrics.timer

systemctl daemon-reload
systemctl enable --now wishkr-nginx-metrics.timer
systemctl start wishkr-nginx-metrics.service

metrics_file=/var/lib/alloy/textfile/wishkr_nginx.prom
if [[ $(stat -c '%U:%G:%a' "${metrics_file}") != root:alloy:640 ]]; then
  echo "ERROR: unexpected Nginx metrics file ownership or mode" >&2
  exit 1
fi

grep -qx 'wishkr_nginx_status_collection_success 1' "${metrics_file}"
grep -qx 'wishkr_nginx_metrics_log_collection_success 1' "${metrics_file}"
grep -qx 'wishkr_nginx_metrics_log_parse_errors 0' "${metrics_file}"

echo "PASS: Nginx monitoring timer installed with loopback-only aggregate status"
