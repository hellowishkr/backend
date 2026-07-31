#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <OVH backup hostname> <OVH backup login>" >&2
  exit 1
fi

raw_host=$1
ftp_user=$2
secrets_dir=/etc/wishkr/secrets
rclone_config=${secrets_dir}/rclone-ovh-backup.conf
credentials_file=${secrets_dir}/ovh-backup.env
password_file=${secrets_dir}/restic_ovh_password

if [[ ! ${raw_host} =~ ^(ftpback-bhs[0-9]+-[0-9]+)\.ip-[0-9-]+\.net$ ]]; then
  echo "ERROR: unexpected OVH BHS Backup Storage hostname" >&2
  exit 1
fi
ftps_host=${BASH_REMATCH[1]}.mybackup.ovh.ca

if [[ ! ${ftp_user} =~ ^ns[0-9]+\.ip-[0-9-]+\.net$ ]]; then
  echo "ERROR: unexpected OVH Backup Storage login" >&2
  exit 1
fi

for command_name in openssl rclone restic; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command not found: ${command_name}" >&2
    exit 1
  fi
done

install -d -o root -g root -m 0700 "${secrets_dir}"

read -r -s -p "OVH Backup Storage password (input hidden): " ftp_password
printf '\n'
if [[ ${#ftp_password} -lt 12 ]]; then
  unset ftp_password
  echo "ERROR: password is unexpectedly short" >&2
  exit 1
fi

obscured_password=$(printf '%s\n' "${ftp_password}" | rclone obscure -)
unset ftp_password

temporary_rclone=$(mktemp "${secrets_dir}/.rclone-ovh-backup.conf.XXXXXX")
temporary_credentials=$(mktemp "${secrets_dir}/.ovh-backup.env.XXXXXX")
cleanup() {
  unset obscured_password
  for temporary_file in "${temporary_rclone:-}" "${temporary_credentials:-}"; do
    if [[ -n ${temporary_file} && -e ${temporary_file} ]]; then
      rm -f -- "${temporary_file}"
    fi
  done
}
trap cleanup EXIT

chmod 0600 "${temporary_rclone}" "${temporary_credentials}"
{
  printf '[ovh-ftps]\n'
  printf 'type = ftp\n'
  printf 'host = %s\n' "${ftps_host}"
  printf 'user = %s\n' "${ftp_user}"
  printf 'pass = %s\n' "${obscured_password}"
  printf 'explicit_tls = true\n'
  printf 'no_check_certificate = false\n'
  printf 'concurrency = 3\n'
} >"${temporary_rclone}"

{
  printf 'OVH_RESTIC_REPOSITORY=/var/lib/wishkr/backup/ovh-restic-repository\n'
  printf 'OVH_RESTIC_PASSWORD_FILE=%s\n' "${password_file}"
  printf 'OVH_RCLONE_DESTINATION=ovh-ftps:wishkr/restic/ovh-prod\n'
  printf 'RCLONE_CONFIG=%s\n' "${rclone_config}"
} >"${temporary_credentials}"
unset obscured_password

chown root:root "${temporary_rclone}" "${temporary_credentials}"
mv -f -- "${temporary_rclone}" "${rclone_config}"
temporary_rclone=
mv -f -- "${temporary_credentials}" "${credentials_file}"
temporary_credentials=
chmod 0600 "${rclone_config}" "${credentials_file}"

if [[ ! -s ${password_file} ]]; then
  if [[ ${WISHKR_EXISTING_RESTIC_REPOSITORY:-0} == 1 ]]; then
    read -r -s -p "Existing OVH restic repository password (input hidden): " restic_password
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

if ! RCLONE_CONFIG="${rclone_config}" rclone lsd ovh-ftps: >/dev/null; then
  echo "ERROR: authenticated FTPS validation failed" >&2
  exit 1
fi

echo "PASS: OVH Backup Storage FTPS credentials installed and validated"
echo "ftps_host=${ftps_host} tls_certificate_verification=enabled"
echo "credentials=${credentials_file} owner=root:root mode=0600"
echo "restic_password=${password_file} owner=root:root mode=0600"
