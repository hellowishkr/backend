#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <R2 endpoint URL> <R2 bucket>" >&2
  exit 1
fi

endpoint=${1%/}
bucket=$2
secrets_dir=/etc/wishkr/secrets
credentials_file=${secrets_dir}/r2-backup.env
password_file=${secrets_dir}/restic_r2_password

if [[ ! ${endpoint} =~ ^https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com$ ]]; then
  echo "ERROR: unexpected Cloudflare R2 endpoint format" >&2
  exit 1
fi

if [[ ! ${bucket} =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]]; then
  echo "ERROR: invalid R2 bucket name" >&2
  exit 1
fi

install -d -o root -g root -m 0700 "${secrets_dir}"

read -r -s -p "R2 Access Key ID (input hidden): " access_key_id
printf '\n'
read -r -s -p "R2 Secret Access Key (input hidden): " secret_access_key
printf '\n'

if [[ ! ${access_key_id} =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
  unset access_key_id secret_access_key
  echo "ERROR: Access Key ID has an unexpected format" >&2
  exit 1
fi

if [[ ! ${secret_access_key} =~ ^[A-Za-z0-9_+/=-]{32,256}$ ]]; then
  unset access_key_id secret_access_key
  echo "ERROR: Secret Access Key has an unexpected format" >&2
  exit 1
fi

temporary_file=$(mktemp "${secrets_dir}/.r2-backup.env.XXXXXX")
cleanup() {
  unset access_key_id secret_access_key
  if [[ -n ${temporary_file:-} && -e ${temporary_file} ]]; then
    rm -f -- "${temporary_file}"
  fi
}
trap cleanup EXIT

chmod 0600 "${temporary_file}"
{
  printf 'AWS_ACCESS_KEY_ID=%s\n' "${access_key_id}"
  printf 'AWS_SECRET_ACCESS_KEY=%s\n' "${secret_access_key}"
  printf 'AWS_DEFAULT_REGION=auto\n'
  printf 'RESTIC_REPOSITORY=s3:%s/%s/restic/ovh-prod\n' "${endpoint}" "${bucket}"
  printf 'RESTIC_PASSWORD_FILE=%s\n' "${password_file}"
} >"${temporary_file}"

chown root:root "${temporary_file}"
mv -f -- "${temporary_file}" "${credentials_file}"
temporary_file=
chmod 0600 "${credentials_file}"

if [[ ! -s ${password_file} ]]; then
  if [[ ${WISHKR_EXISTING_RESTIC_REPOSITORY:-0} == 1 ]]; then
    read -r -s -p "Existing R2 restic repository password (input hidden): " restic_password
    printf '\n'
    if [[ ${#restic_password} -lt 32 ]]; then
      unset restic_password
      echo "ERROR: restic repository password is unexpectedly short" >&2
      exit 1
    fi
    printf '%s\n' "${restic_password}" >"${password_file}"
    unset restic_password
  else
    openssl rand -base64 32 >"${password_file}"
  fi
fi
chown root:root "${password_file}"
chmod 0600 "${password_file}"

unset access_key_id secret_access_key
echo "PASS: R2 backup credentials installed without displaying secret values"
echo "credentials=${credentials_file} owner=root:root mode=0600"
echo "restic_password=${password_file} owner=root:root mode=0600"
