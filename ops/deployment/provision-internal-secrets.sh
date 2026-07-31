#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

secrets_dir=/etc/wishkr/secrets
install -d -o root -g root -m 0700 "${secrets_dir}"

create_secret() {
  local destination=$1
  local temporary

  if [[ -s ${destination} ]]; then
    chown root:root "${destination}"
    chmod 0600 "${destination}"
    return
  fi

  temporary=$(mktemp "${secrets_dir}/.secret.XXXXXX")
  chmod 0600 "${temporary}"
  openssl rand -hex 32 >"${temporary}"
  install -o root -g root -m 0600 "${temporary}" "${destination}"
  rm -f "${temporary}"
}

create_secret "${secrets_dir}/postgres_password"
create_secret "${secrets_dir}/redis_password"

for secret_file in postgres_password redis_password; do
  mode=$(stat -c '%a' "${secrets_dir}/${secret_file}")
  owner=$(stat -c '%U:%G' "${secrets_dir}/${secret_file}")
  if [[ ${mode} != "600" || ${owner} != "root:root" ]]; then
    echo "ERROR: invalid ownership or mode on ${secret_file}" >&2
    exit 1
  fi
done

echo "PASS: PostgreSQL and Redis secrets exist with root:root 0600 protection"
