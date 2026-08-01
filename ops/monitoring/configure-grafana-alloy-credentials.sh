#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

secrets_dir=/etc/wishkr/secrets
credentials_file=${secrets_dir}/grafana-alloy.env

install -d -o root -g root -m 0700 "${secrets_dir}"

read -r -s -p "Grafana Cloud Alloy API token (input hidden): " api_token
printf '\n'

if (( ${#api_token} < 40 || ${#api_token} > 1024 )); then
  unset api_token
  echo "ERROR: token length is outside the expected range" >&2
  exit 1
fi

if [[ ! ${api_token} =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
  unset api_token
  echo "ERROR: token contains unexpected characters" >&2
  exit 1
fi

temporary_file=$(mktemp "${secrets_dir}/.grafana-alloy.env.XXXXXX")
cleanup() {
  unset api_token
  if [[ -n ${temporary_file:-} && -e ${temporary_file} ]]; then
    rm -f -- "${temporary_file}"
  fi
}
trap cleanup EXIT

chmod 0600 "${temporary_file}"
printf 'GCLOUD_RW_API_KEY=%s\n' "${api_token}" >"${temporary_file}"
chown root:root "${temporary_file}"
mv -f -- "${temporary_file}" "${credentials_file}"
temporary_file=
chmod 0600 "${credentials_file}"

unset api_token

if systemctl list-unit-files alloy.service --no-legend 2>/dev/null | grep -q '^alloy.service'; then
  systemctl try-restart alloy.service
fi

echo "PASS: Grafana Alloy credential installed without displaying its value"
echo "credential=${credentials_file} owner=root:root mode=0600"
