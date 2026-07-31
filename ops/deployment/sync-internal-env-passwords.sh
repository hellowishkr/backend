#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

secrets_dir=/etc/wishkr/secrets
environment_file=${secrets_dir}/production.env
postgres_secret=${secrets_dir}/postgres_password
postgres_admin_secret=${secrets_dir}/postgres_admin_password
redis_secret=${secrets_dir}/redis_password

for required_file in "${environment_file}" "${postgres_secret}" "${postgres_admin_secret}" "${redis_secret}"; do
  if [[ ! -s ${required_file} ]]; then
    echo "ERROR: missing required secret file: ${required_file}" >&2
    exit 1
  fi
done

temporary=$(mktemp "${secrets_dir}/.production.env.XXXXXX")
trap 'rm -f "${temporary}"' EXIT
chmod 0600 "${temporary}"

grep -Ev \
  '^(DATABASE_URL|REDIS_URL|POSTGRES_PASSWORD|POSTGRES_ADMIN_PASSWORD|REDIS_PASSWORD)=' \
  "${environment_file}" >"${temporary}"

postgres_password=$(tr -d '\r\n' <"${postgres_secret}")
postgres_admin_password=$(tr -d '\r\n' <"${postgres_admin_secret}")
redis_password=$(tr -d '\r\n' <"${redis_secret}")

{
  printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}"
  printf 'POSTGRES_ADMIN_PASSWORD=%s\n' "${postgres_admin_password}"
  printf 'REDIS_PASSWORD=%s\n' "${redis_password}"
  printf 'DATABASE_URL=postgresql+asyncpg://wishkr:%s@db:5432/wishkr\n' "${postgres_password}"
  printf 'REDIS_URL=redis://:%s@redis:6379/0\n' "${redis_password}"
} >>"${temporary}"

unset postgres_password postgres_admin_password redis_password
install -o root -g root -m 0600 "${temporary}" "${environment_file}"

echo "PASS: internal PostgreSQL and Redis credentials synchronized"
