#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

certificate_path=/etc/wishkr/tls/api.wishkr.com-origin.pem
output_dir=/var/lib/alloy/textfile
output_file=${output_dir}/wishkr_tls_certificate.prom
temporary_file=

cleanup() {
  if [[ -n ${temporary_file:-} && -e ${temporary_file} ]]; then
    rm -f -- "${temporary_file}"
  fi
}
trap cleanup EXIT

if [[ ! -d ${output_dir} ]]; then
  echo "ERROR: Alloy textfile directory does not exist: ${output_dir}" >&2
  exit 1
fi
temporary_file=$(mktemp "${output_file}.tmp.XXXXXX")

cat >"${temporary_file}" <<'EOF'
# HELP wishkr_tls_certificate_collection_success Whether the public certificate was read and its dates parsed successfully.
# TYPE wishkr_tls_certificate_collection_success gauge
# HELP wishkr_tls_certificate_valid_now Whether the current time is within the certificate validity window.
# TYPE wishkr_tls_certificate_valid_now gauge
# HELP wishkr_tls_certificate_hostname_matches Whether the certificate matches api.wishkr.com.
# TYPE wishkr_tls_certificate_hostname_matches gauge
# HELP wishkr_tls_certificate_not_before_timestamp_seconds Unix timestamp at which the certificate becomes valid.
# TYPE wishkr_tls_certificate_not_before_timestamp_seconds gauge
# HELP wishkr_tls_certificate_not_after_timestamp_seconds Unix timestamp at which the certificate expires.
# TYPE wishkr_tls_certificate_not_after_timestamp_seconds gauge
# HELP wishkr_tls_certificate_seconds_until_expiry Seconds remaining until certificate expiry; negative means expired.
# TYPE wishkr_tls_certificate_seconds_until_expiry gauge
# HELP wishkr_tls_certificate_last_collection_timestamp_seconds Unix timestamp of the last certificate collection attempt.
# TYPE wishkr_tls_certificate_last_collection_timestamp_seconds gauge
EOF

now_epoch=$(date -u +%s)
collection_success=0
valid_now=0
hostname_matches=0
certificate_dates=

if [[ -s ${certificate_path} ]]; then
  certificate_dates=$(openssl x509 -in "${certificate_path}" -noout -startdate -enddate 2>/dev/null) || \
    certificate_dates=
  if openssl x509 -in "${certificate_path}" -noout -checkhost api.wishkr.com \
    >/dev/null 2>&1; then
    hostname_matches=1
  fi
fi

not_before_text=$(sed -n 's/^notBefore=//p' <<<"${certificate_dates}")
not_after_text=$(sed -n 's/^notAfter=//p' <<<"${certificate_dates}")
not_before_epoch=$(date -u --date="${not_before_text}" +%s 2>/dev/null) || not_before_epoch=
not_after_epoch=$(date -u --date="${not_after_text}" +%s 2>/dev/null) || not_after_epoch=

if [[ ${not_before_epoch} =~ ^[0-9]+$ && ${not_after_epoch} =~ ^[0-9]+$ ]] &&
  (( not_after_epoch > not_before_epoch )); then
  collection_success=1
  if (( now_epoch >= not_before_epoch && now_epoch < not_after_epoch )); then
    valid_now=1
  fi
  seconds_until_expiry=$((not_after_epoch - now_epoch))

  printf 'wishkr_tls_certificate_not_before_timestamp_seconds{certificate="api-origin"} %s\n' \
    "${not_before_epoch}" >>"${temporary_file}"
  printf 'wishkr_tls_certificate_not_after_timestamp_seconds{certificate="api-origin"} %s\n' \
    "${not_after_epoch}" >>"${temporary_file}"
  printf 'wishkr_tls_certificate_seconds_until_expiry{certificate="api-origin"} %s\n' \
    "${seconds_until_expiry}" >>"${temporary_file}"
fi

printf 'wishkr_tls_certificate_collection_success{certificate="api-origin"} %d\n' \
  "${collection_success}" >>"${temporary_file}"
printf 'wishkr_tls_certificate_valid_now{certificate="api-origin"} %d\n' \
  "${valid_now}" >>"${temporary_file}"
printf 'wishkr_tls_certificate_hostname_matches{certificate="api-origin"} %d\n' \
  "${hostname_matches}" >>"${temporary_file}"
printf 'wishkr_tls_certificate_last_collection_timestamp_seconds %s\n' \
  "${now_epoch}" >>"${temporary_file}"

chown root:alloy "${temporary_file}"
chmod 0640 "${temporary_file}"
mv -f -- "${temporary_file}" "${output_file}"
temporary_file=

if (( collection_success != 1 || valid_now != 1 || hostname_matches != 1 )); then
  echo "ERROR: origin certificate validation failed" >&2
  exit 1
fi

echo "PASS: exported public origin certificate validity and expiry metrics"
