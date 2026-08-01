#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

status_endpoint=http://127.0.0.1:8081/nginx_status
metrics_log=/var/log/nginx/wishkr-api-metrics.log
output_dir=/var/lib/alloy/textfile
output_file=${output_dir}/wishkr_nginx.prom
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
# HELP wishkr_nginx_status_collection_success Whether the loopback Nginx stub-status response was collected and parsed.
# TYPE wishkr_nginx_status_collection_success gauge
# HELP wishkr_nginx_connections_active Current active Nginx client connections.
# TYPE wishkr_nginx_connections_active gauge
# HELP wishkr_nginx_connections_accepted_total Total Nginx client connections accepted since process start.
# TYPE wishkr_nginx_connections_accepted_total counter
# HELP wishkr_nginx_connections_handled_total Total Nginx client connections handled since process start.
# TYPE wishkr_nginx_connections_handled_total counter
# HELP wishkr_nginx_requests_total Total Nginx client requests processed since process start.
# TYPE wishkr_nginx_requests_total counter
# HELP wishkr_nginx_connections_reading Current Nginx connections reading request headers.
# TYPE wishkr_nginx_connections_reading gauge
# HELP wishkr_nginx_connections_writing Current Nginx connections writing responses.
# TYPE wishkr_nginx_connections_writing gauge
# HELP wishkr_nginx_connections_waiting Current idle Nginx keepalive connections.
# TYPE wishkr_nginx_connections_waiting gauge
# HELP wishkr_nginx_metrics_log_collection_success Whether the status-only metrics log was available and parsed without malformed lines.
# TYPE wishkr_nginx_metrics_log_collection_success gauge
# HELP wishkr_nginx_metrics_log_parse_errors Number of malformed lines in the current status-only metrics-log inputs.
# TYPE wishkr_nginx_metrics_log_parse_errors gauge
# HELP wishkr_nginx_http_requests_5m Requests recorded during the last five minutes.
# TYPE wishkr_nginx_http_requests_5m gauge
# HELP wishkr_nginx_http_4xx_requests_5m HTTP 4xx responses recorded during the last five minutes.
# TYPE wishkr_nginx_http_4xx_requests_5m gauge
# HELP wishkr_nginx_http_5xx_requests_5m HTTP 5xx responses recorded during the last five minutes.
# TYPE wishkr_nginx_http_5xx_requests_5m gauge
# HELP wishkr_nginx_http_429_requests_5m HTTP 429 responses recorded during the last five minutes.
# TYPE wishkr_nginx_http_429_requests_5m gauge
# HELP wishkr_nginx_http_499_requests_5m Nginx client-closed-request responses recorded during the last five minutes.
# TYPE wishkr_nginx_http_499_requests_5m gauge
# HELP wishkr_nginx_last_collection_timestamp_seconds Unix timestamp of the last Nginx collection attempt.
# TYPE wishkr_nginx_last_collection_timestamp_seconds gauge
EOF

collection_failed=0
status_collection_success=0
status_body=$(curl --fail --silent --show-error --max-time 5 "${status_endpoint}" 2>/dev/null) || \
  status_body=
active_connections=$(awk '$1 == "Active" && $2 == "connections:" {print $3; exit}' <<<"${status_body}")
connection_totals=$(awk '
  $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && NF == 3 {
    print $1 "|" $2 "|" $3
    exit
  }
' <<<"${status_body}")
connection_states=$(awk '
  $1 == "Reading:" && $3 == "Writing:" && $5 == "Waiting:" {
    print $2 "|" $4 "|" $6
    exit
  }
' <<<"${status_body}")

if [[ ${active_connections} =~ ^[0-9]+$ &&
  ${connection_totals} =~ ^([0-9]+)\|([0-9]+)\|([0-9]+)$ ]]; then
  accepted_connections=${BASH_REMATCH[1]}
  handled_connections=${BASH_REMATCH[2]}
  total_requests=${BASH_REMATCH[3]}
  if [[ ${connection_states} =~ ^([0-9]+)\|([0-9]+)\|([0-9]+)$ ]]; then
    reading_connections=${BASH_REMATCH[1]}
    writing_connections=${BASH_REMATCH[2]}
    waiting_connections=${BASH_REMATCH[3]}
    status_collection_success=1
    printf 'wishkr_nginx_connections_active %s\n' "${active_connections}" >>"${temporary_file}"
    printf 'wishkr_nginx_connections_accepted_total %s\n' "${accepted_connections}" >>"${temporary_file}"
    printf 'wishkr_nginx_connections_handled_total %s\n' "${handled_connections}" >>"${temporary_file}"
    printf 'wishkr_nginx_requests_total %s\n' "${total_requests}" >>"${temporary_file}"
    printf 'wishkr_nginx_connections_reading %s\n' "${reading_connections}" >>"${temporary_file}"
    printf 'wishkr_nginx_connections_writing %s\n' "${writing_connections}" >>"${temporary_file}"
    printf 'wishkr_nginx_connections_waiting %s\n' "${waiting_connections}" >>"${temporary_file}"
  fi
fi
if (( status_collection_success != 1 )); then
  collection_failed=1
fi
printf 'wishkr_nginx_status_collection_success %d\n' \
  "${status_collection_success}" >>"${temporary_file}"

log_files=()
for candidate in "${metrics_log}.1" "${metrics_log}"; do
  if [[ -r ${candidate} ]]; then
    log_files+=("${candidate}")
  fi
done

log_collection_success=0
requests_5m=0
responses_4xx_5m=0
responses_5xx_5m=0
responses_429_5m=0
responses_499_5m=0
parse_errors=0
if (( ${#log_files[@]} > 0 )); then
  cutoff_epoch=$(( $(date -u +%s) - 300 ))
  log_counts=$(awk -v cutoff="${cutoff_epoch}" '
    NF == 0 { next }
    $1 !~ /^[0-9]+([.][0-9]+)?$/ || $2 !~ /^[0-9][0-9][0-9]$/ {
      parse_errors++
      next
    }
    ($1 + 0) >= cutoff {
      requests++
      status = $2 + 0
      if (status >= 400 && status < 500) responses_4xx++
      if (status >= 500 && status < 600) responses_5xx++
      if (status == 429) responses_429++
      if (status == 499) responses_499++
    }
    END {
      print (requests + 0) "|" (responses_4xx + 0) "|" \
        (responses_5xx + 0) "|" (responses_429 + 0) "|" \
        (responses_499 + 0) "|" (parse_errors + 0)
    }
  ' "${log_files[@]}")

  if [[ ${log_counts} =~ ^([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)$ ]]; then
    requests_5m=${BASH_REMATCH[1]}
    responses_4xx_5m=${BASH_REMATCH[2]}
    responses_5xx_5m=${BASH_REMATCH[3]}
    responses_429_5m=${BASH_REMATCH[4]}
    responses_499_5m=${BASH_REMATCH[5]}
    parse_errors=${BASH_REMATCH[6]}
    if (( parse_errors == 0 )); then
      log_collection_success=1
    fi
  fi
fi
if (( log_collection_success != 1 )); then
  collection_failed=1
fi

printf 'wishkr_nginx_metrics_log_collection_success %d\n' \
  "${log_collection_success}" >>"${temporary_file}"
printf 'wishkr_nginx_metrics_log_parse_errors %d\n' "${parse_errors}" >>"${temporary_file}"
printf 'wishkr_nginx_http_requests_5m %d\n' "${requests_5m}" >>"${temporary_file}"
printf 'wishkr_nginx_http_4xx_requests_5m %d\n' "${responses_4xx_5m}" >>"${temporary_file}"
printf 'wishkr_nginx_http_5xx_requests_5m %d\n' "${responses_5xx_5m}" >>"${temporary_file}"
printf 'wishkr_nginx_http_429_requests_5m %d\n' "${responses_429_5m}" >>"${temporary_file}"
printf 'wishkr_nginx_http_499_requests_5m %d\n' "${responses_499_5m}" >>"${temporary_file}"
printf 'wishkr_nginx_last_collection_timestamp_seconds %(%s)T\n' -1 >>"${temporary_file}"

chown root:alloy "${temporary_file}"
chmod 0640 "${temporary_file}"
mv -f -- "${temporary_file}" "${output_file}"
temporary_file=

if (( collection_failed != 0 )); then
  echo "ERROR: Nginx status or metrics-log collection failed" >&2
  exit 1
fi

echo "PASS: exported aggregate Nginx status, volume, and response metrics"
