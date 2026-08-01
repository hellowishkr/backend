#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

credentials_file=/etc/wishkr/secrets/grafana-alloy.env
metrics_url=http://127.0.0.1:12345/metrics

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ -s ${credentials_file} ]] || fail "missing Grafana credential"
[[ $(stat -c '%U:%G:%a' "${credentials_file}") == root:root:600 ]] || \
  fail "Grafana credential permissions are not root:root 0600"
[[ $(stat -c '%U:%G:%a' /etc/alloy/config.alloy) == root:alloy:640 ]] || \
  fail "Alloy configuration permissions are not root:alloy 0640"

set -a
# shellcheck disable=SC1090
source "${credentials_file}"
set +a
/usr/bin/alloy fmt --test /etc/alloy/config.alloy
/usr/bin/alloy validate /etc/alloy/config.alloy
unset GCLOUD_RW_API_KEY

[[ $(systemctl is-active alloy.service) == active ]] || fail "alloy.service is not active"
[[ $(systemctl is-enabled alloy.service) == enabled ]] || fail "alloy.service is not enabled"
[[ $(systemctl show alloy.service -p User --value) == alloy ]] || fail "Alloy is not configured as user alloy"
[[ $(systemctl show alloy.service -p Group --value) == alloy ]] || fail "Alloy is not configured as group alloy"

for prohibited_group in docker adm systemd-journal; do
  if id -nG alloy | tr ' ' '\n' | grep -qx "${prohibited_group}"; then
    fail "alloy has prohibited ${prohibited_group} group membership"
  fi
done

[[ -z $(systemctl show alloy.service -p SupplementaryGroups --value) ]] || \
  fail "Alloy has explicit supplementary groups"

curl --fail --silent --show-error http://127.0.0.1:12345/-/ready >/dev/null
curl --fail --silent --show-error http://127.0.0.1:12345/-/healthy >/dev/null

if ! ss -lntH 'sport = :12345' | awk '{print $4}' | grep -qx '127.0.0.1:12345'; then
  fail "Alloy UI is not bound exclusively to IPv4 loopback"
fi

if ss -lntH 'sport = :12345' | awk '{print $4}' | grep -Eq '^(0\.0\.0\.0|\[::\]|\*):12345$'; then
  fail "Alloy UI has a public or wildcard listener"
fi

if grep -Eq '(^|[[:space:]])loki\.' /etc/alloy/config.alloy; then
  fail "Loki log collection is unexpectedly configured"
fi

metrics=$(curl --fail --silent --show-error "${metrics_url}")
sent_samples=$(awk '
  /^prometheus_remote_storage_samples_total([{ ]|$)/ { total += $NF }
  END { printf "%.0f", total + 0 }
' <<<"${metrics}")
failed_samples=$(awk '
  /^prometheus_remote_storage_samples_failed_total([{ ]|$)/ { total += $NF }
  END { printf "%.0f", total + 0 }
' <<<"${metrics}")

if journalctl -u alloy.service --since '-10 minutes' --no-pager \
  | grep -Eqi '401 Unauthorized|authentication error|non-recoverable error'; then
  fail "recent Alloy logs contain an authentication or remote-write error"
fi

echo "PASS: Alloy service, permissions, local-only listener, and metrics pipeline validated"
echo "remote_write_samples_sent=${sent_samples}"
echo "remote_write_samples_failed=${failed_samples}"
echo "NOTE: confirm host wishkr-prod-mum-01 and alert rules in Grafana Cloud"
