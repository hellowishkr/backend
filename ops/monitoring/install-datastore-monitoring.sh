#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

for required_file in \
  export-datastore-metrics.sh \
  wishkr-datastore-metrics.service \
  wishkr-datastore-metrics.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in docker awk grep; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command is not installed: ${command_name}" >&2
    exit 1
  fi
done

if ! getent group alloy >/dev/null; then
  echo "ERROR: Alloy must be installed before datastore metric collection" >&2
  exit 1
fi

install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -o root -g root -m 0750 \
  "${source_dir}/export-datastore-metrics.sh" \
  /usr/local/sbin/wishkr-export-datastore-metrics
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-datastore-metrics.service" \
  /etc/systemd/system/wishkr-datastore-metrics.service
install -o root -g root -m 0644 \
  "${source_dir}/wishkr-datastore-metrics.timer" \
  /etc/systemd/system/wishkr-datastore-metrics.timer

systemctl daemon-reload
systemctl enable --now wishkr-datastore-metrics.timer
systemctl start wishkr-datastore-metrics.service

metrics_file=/var/lib/alloy/textfile/wishkr_datastores.prom
if [[ $(stat -c '%U:%G:%a' "${metrics_file}") != root:alloy:640 ]]; then
  echo "ERROR: unexpected datastore metrics file ownership or mode" >&2
  exit 1
fi

grep -qx 'wishkr_datastore_docker_available 1' "${metrics_file}"
grep -qx 'wishkr_datastore_collection_success{datastore="postgresql"} 1' "${metrics_file}"
grep -qx 'wishkr_datastore_ready{datastore="postgresql"} 1' "${metrics_file}"
grep -qx 'wishkr_postgresql_in_recovery 0' "${metrics_file}"
grep -qx 'wishkr_postgresql_data_checksums_enabled 1' "${metrics_file}"
grep -qx 'wishkr_datastore_collection_success{datastore="redis"} 1' "${metrics_file}"
grep -qx 'wishkr_datastore_ready{datastore="redis"} 1' "${metrics_file}"
grep -qx 'wishkr_redis_loading 0' "${metrics_file}"
grep -qx 'wishkr_redis_rdb_last_bgsave_success 1' "${metrics_file}"
grep -qx 'wishkr_redis_aof_enabled 1' "${metrics_file}"
grep -qx 'wishkr_redis_aof_last_bgrewrite_success 1' "${metrics_file}"
grep -qx 'wishkr_redis_aof_last_write_success 1' "${metrics_file}"

echo "PASS: datastore metrics timer installed and PostgreSQL/Redis are healthy"
