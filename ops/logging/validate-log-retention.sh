#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

cmp -s "${source_dir}/10-wishkr-journald.conf" \
  /etc/systemd/journald.conf.d/10-wishkr.conf || \
  fail "installed journald policy differs from reviewed source"
cmp -s "${source_dir}/nginx.logrotate" /etc/logrotate.d/nginx || \
  fail "installed Nginx rotation policy differs from reviewed source"
cmp -s "${source_dir}/wishkr-logs.tmpfiles" \
  /etc/tmpfiles.d/wishkr-logs.conf || \
  fail "installed Wishkr cleanup policy differs from reviewed source"

[[ $(systemctl is-active systemd-journald.service) == active ]] || \
  fail "systemd-journald is not active"
[[ $(systemctl is-active nginx.service) == active ]] || \
  fail "Nginx is not active"
[[ $(systemctl is-active docker.service) == active ]] || \
  fail "Docker is not active"
[[ $(systemctl is-active auditd.service) == active ]] || \
  fail "auditd is not active"
[[ $(systemctl is-active systemd-tmpfiles-clean.timer) == active ]] || \
  fail "systemd-tmpfiles-clean.timer is not active"

effective_journal=$(systemd-analyze cat-config systemd/journald.conf)
for required_setting in \
    Storage=persistent \
    Compress=yes \
    Seal=yes \
    SystemMaxUse=1G \
    SystemKeepFree=2G \
    MaxRetentionSec=30day \
    MaxFileSec=1day; do
  grep -qx "${required_setting}" <<<"${effective_journal}" || \
    fail "missing effective journald setting ${required_setting}"
done

[[ $(stat -c '%U:%G:%a' /var/log/wishkr) == root:adm:750 ]] || \
  fail "/var/log/wishkr permissions are not root:adm 0750"
[[ $(stat -c '%U:%G:%a' /var/log/wishkr-trivy) == root:adm:750 ]] || \
  fail "/var/log/wishkr-trivy permissions are not root:adm 0750"

for log_path in \
    /var/log/nginx/wishkr-api-access.log \
    /var/log/nginx/wishkr-api-error.log \
    /var/log/nginx/wishkr-api-metrics.log; do
  if [[ -e ${log_path} && $(stat -c '%U:%G:%a' "${log_path}") != root:adm:640 ]]; then
    fail "${log_path} permissions are not root:adm 0640"
  fi
done

for prohibited_group in docker adm systemd-journal; do
  if id -nG alloy 2>/dev/null | tr ' ' '\n' | grep -qx "${prohibited_group}"; then
    fail "alloy has prohibited ${prohibited_group} group membership"
  fi
done

if grep -Eq '(^|[[:space:]])loki\.' /etc/alloy/config.alloy; then
  fail "Loki log collection is unexpectedly configured"
fi

for container in \
    wishkr-db-1 \
    wishkr-redis-1 \
    wishkr-backend-1 \
    wishkr-scheduler-1; do
  [[ $(docker inspect --format '{{.HostConfig.LogConfig.Type}}' "${container}") == json-file ]] || \
    fail "${container} does not use the json-file logging driver"
  log_options=$(docker inspect --format '{{json .HostConfig.LogConfig.Config}}' "${container}")
  grep -q '"compress":"true"' <<<"${log_options}" || \
    fail "${container} log compression is not enabled"
  grep -q '"max-file":"5"' <<<"${log_options}" || \
    fail "${container} max-file is not 5"
  grep -q '"max-size":"10m"' <<<"${log_options}" || \
    fail "${container} max-size is not 10m"
done

audit_config=/etc/audit/auditd.conf
grep -Eq '^[[:space:]]*max_log_file[[:space:]]*=[[:space:]]*64[[:space:]]*$' "${audit_config}" || \
  fail "auditd max_log_file is not 64 MiB"
grep -Eq '^[[:space:]]*num_logs[[:space:]]*=[[:space:]]*8[[:space:]]*$' "${audit_config}" || \
  fail "auditd num_logs is not 8"
grep -Eq '^[[:space:]]*max_log_file_action[[:space:]]*=[[:space:]]*ROTATE[[:space:]]*$' "${audit_config}" || \
  fail "auditd is not configured to rotate"

logrotate --debug /etc/logrotate.conf >/dev/null 2>&1
journalctl --verify --quiet >/dev/null 2>&1

echo "PASS: important logs are locally protected with bounded retention"
echo "journald_retention=30_days journald_max=1_GiB"
echo "nginx_retention=14_days"
echo "container_retention=5x10_MiB_per_container"
echo "audit_retention=8x64_MiB"
echo "wishkr_operational_retention=30_days trivy_retention=90_days"
echo "loki_enabled=false alloy_log_access=false"
