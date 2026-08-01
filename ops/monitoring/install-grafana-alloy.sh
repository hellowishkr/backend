#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
alloy_version=1.17.1
package_name=alloy-${alloy_version}-1.amd64.deb
package_url=https://github.com/grafana/alloy/releases/download/v${alloy_version}/${package_name}
package_sha256=79df25ef60db9192d2a679832fd7d8e6e38f097359590f170618ce0ecd342d1b
credentials_file=/etc/wishkr/secrets/grafana-alloy.env
temporary_dir=

cleanup() {
  if [[ -n ${temporary_dir:-} && -d ${temporary_dir} ]]; then
    rm -rf -- "${temporary_dir}"
  fi
}
trap cleanup EXIT

for required_file in config.alloy alloy.default alloy-hardening.conf; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

if [[ ! -s ${credentials_file} ]]; then
  echo "ERROR: install the Grafana credential first" >&2
  echo "Run: sudo ${source_dir}/configure-grafana-alloy-credentials.sh" >&2
  exit 1
fi

if [[ $(stat -c '%U:%G:%a' "${credentials_file}") != root:root:600 ]]; then
  echo "ERROR: ${credentials_file} must be root:root mode 0600" >&2
  exit 1
fi

if [[ $(dpkg --print-architecture) != amd64 ]]; then
  echo "ERROR: this pinned installer supports amd64 only" >&2
  exit 1
fi

installed_version=
if dpkg-query -W -f='${Version}' alloy >/dev/null 2>&1; then
  installed_version=$(dpkg-query -W -f='${Version}' alloy)
fi

if [[ -n ${installed_version} && ${installed_version} != "${alloy_version}-1" ]]; then
  echo "ERROR: Alloy ${installed_version} is installed; reviewed target is ${alloy_version}-1" >&2
  exit 1
fi

if [[ -z ${installed_version} ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl

  temporary_dir=$(mktemp -d /tmp/wishkr-alloy-install.XXXXXX)
  package_path=${temporary_dir}/${package_name}

  curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    "${package_url}" \
    --output "${package_path}"

  printf '%s  %s\n' "${package_sha256}" "${package_path}" | sha256sum --check --status
  echo "PASS: verified Grafana Alloy package SHA-256"

  apt-get install -y --no-install-recommends "${package_path}"
  installed_version=$(dpkg-query -W -f='${Version}' alloy)
else
  echo "PASS: reviewed Grafana Alloy version ${installed_version} is already installed"
fi

if [[ ${installed_version} != "${alloy_version}-1" ]]; then
  echo "ERROR: unexpected installed Alloy version: ${installed_version}" >&2
  exit 1
fi

install -d -o root -g alloy -m 0750 /etc/alloy
install -d -o alloy -g alloy -m 0750 /var/lib/alloy
install -d -o alloy -g alloy -m 0750 /var/lib/alloy/data
install -d -o root -g alloy -m 0750 /var/lib/alloy/textfile
install -d -o root -g root -m 0755 /etc/systemd/system/alloy.service.d

install -o root -g alloy -m 0640 "${source_dir}/config.alloy" /etc/alloy/config.alloy
install -o root -g root -m 0644 "${source_dir}/alloy.default" /etc/default/alloy
install -o root -g root -m 0644 \
  "${source_dir}/alloy-hardening.conf" \
  /etc/systemd/system/alloy.service.d/10-wishkr-hardening.conf

/usr/bin/alloy fmt --test /etc/alloy/config.alloy
set -a
# shellcheck disable=SC1090
source "${credentials_file}"
set +a
/usr/bin/alloy validate /etc/alloy/config.alloy
unset GCLOUD_RW_API_KEY

# The upstream package may add Alloy to local log-reader groups for generic
# Loki deployments. Wishkr runs Alloy as a metrics-only collector, so retaining
# those memberships would unnecessarily expose journald, Nginx, and audit logs.
for prohibited_group in docker adm systemd-journal; do
  if id -nG alloy | tr ' ' '\n' | grep -qx "${prohibited_group}"; then
    gpasswd --delete alloy "${prohibited_group}"
  fi
done

for prohibited_group in docker adm systemd-journal; do
  if id -nG alloy | tr ' ' '\n' | grep -qx "${prohibited_group}"; then
    echo "ERROR: alloy retains prohibited ${prohibited_group} group membership" >&2
    exit 1
  fi
done

systemctl daemon-reload
systemctl enable alloy.service
systemctl restart alloy.service

for _ in {1..12}; do
  if curl --fail --silent --show-error http://127.0.0.1:12345/-/ready >/dev/null; then
    break
  fi
  sleep 1
done

curl --fail --silent --show-error http://127.0.0.1:12345/-/ready
curl --fail --silent --show-error http://127.0.0.1:12345/-/healthy

echo "PASS: Grafana Alloy ${installed_version} installed and healthy"
echo "Next: run sudo ${source_dir}/validate-grafana-alloy.sh"
