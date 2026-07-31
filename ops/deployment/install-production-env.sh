#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

incoming=${1:-}
if [[ -z ${incoming} || ! -f ${incoming} ]]; then
  echo "ERROR: usage: install-production-env.sh INCOMING_ENV_FILE" >&2
  exit 1
fi

secrets_dir=/etc/wishkr/secrets
destination=${secrets_dir}/production.env
postgres_secret=${secrets_dir}/postgres_password
redis_secret=${secrets_dir}/redis_password

for secret_file in "${postgres_secret}" "${redis_secret}"; do
  if [[ ! -s ${secret_file} ]]; then
    echo "ERROR: missing internal secret: ${secret_file}" >&2
    exit 1
  fi
done

if LC_ALL=C grep -q $'\r' "${incoming}"; then
  echo "ERROR: incoming environment file contains CRLF line endings" >&2
  exit 1
fi

duplicates=$(
  sed -n -E 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1/p' "${incoming}" |
    sort |
    uniq -d
)
if [[ -n ${duplicates} ]]; then
  echo "ERROR: incoming environment file contains duplicate keys" >&2
  printf '%s\n' "${duplicates}" >&2
  exit 1
fi

required=(
  GEMINI_API_KEY
  WHATSAPP_ACCESS_TOKEN
  WHATSAPP_PHONE_NUMBER_ID
  WHATSAPP_VERIFY_TOKEN
  WHATSAPP_WEBHOOK_SECRET
  RAZORPAY_KEY_ID
  RAZORPAY_KEY_SECRET
  RAZORPAY_WEBHOOK_SECRET
  FOUNDER_PHONE
  R2_ENDPOINT
  R2_BUCKET
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_PUBLIC_BASE_URL
)
for key in "${required[@]}"; do
  if ! grep -Eq "^${key}=.+" "${incoming}"; then
    echo "ERROR: required production key is missing or empty: ${key}" >&2
    exit 1
  fi
done

temporary=$(mktemp "${secrets_dir}/.production.env.XXXXXX")
trap 'rm -f "${temporary}"' EXIT
chmod 0600 "${temporary}"

grep -Ev \
  '^(APP_ENV|APP_ROLE|RUN_SCHEDULER|WEB_WORKERS|ALLOWED_HOSTS|GIFT_REFINEMENT_ENABLED|DATABASE_URL|REDIS_URL|POSTGRES_PASSWORD|POSTGRES_ADMIN_PASSWORD|REDIS_PASSWORD|POSTGRES_USER|POSTGRES_DB|WISHKR_ENV_FILE|WISHKR_SECRETS_DIR)=' \
  "${incoming}" >"${temporary}"

postgres_password=$(tr -d '\r\n' <"${postgres_secret}")
redis_password=$(tr -d '\r\n' <"${redis_secret}")

{
  printf '\n# OVH production deployment overrides\n'
  printf 'APP_ENV=production\n'
  printf 'APP_ROLE=web\n'
  printf 'RUN_SCHEDULER=false\n'
  printf 'WEB_WORKERS=1\n'
  printf 'ALLOWED_HOSTS=api.wishkr.com,wishkr.com,www.wishkr.com,127.0.0.1,localhost\n'
  printf 'GIFT_REFINEMENT_ENABLED=true\n'
  printf 'POSTGRES_USER=wishkr\n'
  printf 'POSTGRES_DB=wishkr\n'
  printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}"
  printf 'REDIS_PASSWORD=%s\n' "${redis_password}"
  printf 'DATABASE_URL=postgresql+asyncpg://wishkr:%s@db:5432/wishkr\n' "${postgres_password}"
  printf 'REDIS_URL=redis://:%s@redis:6379/0\n' "${redis_password}"
  printf 'WISHKR_ENV_FILE=/etc/wishkr/secrets/production.env\n'
  printf 'WISHKR_SECRETS_DIR=/etc/wishkr/secrets\n'
} >>"${temporary}"

unset postgres_password redis_password

install -o root -g root -m 0600 "${temporary}" "${destination}"
rm -f "${incoming}"

mode=$(stat -c '%a' "${destination}")
owner=$(stat -c '%U:%G' "${destination}")
if [[ ${mode} != "600" || ${owner} != "root:root" ]]; then
  echo "ERROR: production.env ownership or mode validation failed" >&2
  exit 1
fi

echo "PASS: production environment installed with root:root 0600 protection"
