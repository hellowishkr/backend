#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

output_dir=/var/lib/alloy/textfile
output_file=${output_dir}/wishkr_nvme_smart.prom
temporary_file=

cleanup() {
  if [[ -n ${temporary_file:-} && -e ${temporary_file} ]]; then
    rm -f -- "${temporary_file}"
  fi
}
trap cleanup EXIT

install -d -o root -g alloy -m 0750 "${output_dir}"
temporary_file=$(mktemp "${output_file}.tmp.XXXXXX")

cat >"${temporary_file}" <<'EOF'
# HELP wishkr_nvme_smart_devices_detected Number of NVMe namespace devices detected.
# TYPE wishkr_nvme_smart_devices_detected gauge
# HELP wishkr_nvme_smart_collection_success Whether SMART data collection and parsing succeeded for the device.
# TYPE wishkr_nvme_smart_collection_success gauge
# HELP wishkr_nvme_smart_passed Whether the device reports an overall passing SMART health status.
# TYPE wishkr_nvme_smart_passed gauge
# HELP wishkr_nvme_smart_critical_warning NVMe SMART critical warning bitmask; zero is healthy.
# TYPE wishkr_nvme_smart_critical_warning gauge
# HELP wishkr_nvme_smart_temperature_celsius Current composite NVMe temperature in Celsius.
# TYPE wishkr_nvme_smart_temperature_celsius gauge
# HELP wishkr_nvme_smart_available_spare_percent Available NVMe spare capacity percentage.
# TYPE wishkr_nvme_smart_available_spare_percent gauge
# HELP wishkr_nvme_smart_spare_threshold_percent NVMe spare-capacity warning threshold percentage.
# TYPE wishkr_nvme_smart_spare_threshold_percent gauge
# HELP wishkr_nvme_smart_percentage_used Estimated NVMe endurance consumed percentage.
# TYPE wishkr_nvme_smart_percentage_used gauge
# HELP wishkr_nvme_smart_media_errors_total Unrecoverable NVMe media and data-integrity errors.
# TYPE wishkr_nvme_smart_media_errors_total counter
# HELP wishkr_nvme_smart_error_log_entries_total NVMe error-information log entries.
# TYPE wishkr_nvme_smart_error_log_entries_total counter
# HELP wishkr_nvme_smart_unsafe_shutdowns_total NVMe unsafe shutdown count.
# TYPE wishkr_nvme_smart_unsafe_shutdowns_total counter
# HELP wishkr_nvme_smart_power_on_hours_total NVMe power-on hours.
# TYPE wishkr_nvme_smart_power_on_hours_total counter
# HELP wishkr_nvme_smart_warning_temperature_minutes_total Minutes spent above the warning temperature.
# TYPE wishkr_nvme_smart_warning_temperature_minutes_total counter
# HELP wishkr_nvme_smart_critical_temperature_minutes_total Minutes spent above the critical temperature.
# TYPE wishkr_nvme_smart_critical_temperature_minutes_total counter
# HELP wishkr_nvme_smart_last_collection_timestamp_seconds Unix timestamp of the last completed collection attempt.
# TYPE wishkr_nvme_smart_last_collection_timestamp_seconds gauge
EOF

shopt -s nullglob
devices=(/dev/nvme*n1)
printf 'wishkr_nvme_smart_devices_detected %d\n' "${#devices[@]}" >>"${temporary_file}"

collection_failed=0
for device_path in "${devices[@]}"; do
  device=${device_path##*/}

  set +e
  smart_json=$(smartctl --json --all "${device_path}" 2>/dev/null)
  set -e

  values=$(jq --exit-status --raw-output '
    [
      (if .smart_status.passed == true then 1 else 0 end),
      (.nvme_smart_health_information_log.critical_warning | numbers),
      (.nvme_smart_health_information_log.temperature | numbers),
      (.nvme_smart_health_information_log.available_spare | numbers),
      (.nvme_smart_health_information_log.available_spare_threshold | numbers),
      (.nvme_smart_health_information_log.percentage_used | numbers),
      (.nvme_smart_health_information_log.media_errors | numbers),
      (.nvme_smart_health_information_log.num_err_log_entries | numbers),
      (.nvme_smart_health_information_log.unsafe_shutdowns | numbers),
      (.nvme_smart_health_information_log.power_on_hours | numbers),
      (.nvme_smart_health_information_log.warning_temp_time | numbers),
      (.nvme_smart_health_information_log.critical_comp_time | numbers)
    ] | @tsv
  ' <<<"${smart_json}" 2>/dev/null) || values=

  if [[ -z ${values} ]]; then
    printf 'wishkr_nvme_smart_collection_success{device="%s"} 0\n' "${device}" \
      >>"${temporary_file}"
    collection_failed=1
    continue
  fi

  IFS=$'\t' read -r \
    smart_passed critical_warning temperature available_spare spare_threshold \
    percentage_used media_errors error_log_entries unsafe_shutdowns \
    power_on_hours warning_temp_time critical_temp_time <<<"${values}"

  printf 'wishkr_nvme_smart_collection_success{device="%s"} 1\n' "${device}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_passed{device="%s"} %s\n' "${device}" "${smart_passed}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_critical_warning{device="%s"} %s\n' "${device}" "${critical_warning}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_temperature_celsius{device="%s"} %s\n' "${device}" "${temperature}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_available_spare_percent{device="%s"} %s\n' "${device}" "${available_spare}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_spare_threshold_percent{device="%s"} %s\n' "${device}" "${spare_threshold}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_percentage_used{device="%s"} %s\n' "${device}" "${percentage_used}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_media_errors_total{device="%s"} %s\n' "${device}" "${media_errors}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_error_log_entries_total{device="%s"} %s\n' "${device}" "${error_log_entries}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_unsafe_shutdowns_total{device="%s"} %s\n' "${device}" "${unsafe_shutdowns}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_power_on_hours_total{device="%s"} %s\n' "${device}" "${power_on_hours}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_warning_temperature_minutes_total{device="%s"} %s\n' "${device}" "${warning_temp_time}" >>"${temporary_file}"
  printf 'wishkr_nvme_smart_critical_temperature_minutes_total{device="%s"} %s\n' "${device}" "${critical_temp_time}" >>"${temporary_file}"
done

printf 'wishkr_nvme_smart_last_collection_timestamp_seconds %(%s)T\n' -1 >>"${temporary_file}"

chown root:alloy "${temporary_file}"
chmod 0640 "${temporary_file}"
mv -f -- "${temporary_file}" "${output_file}"
temporary_file=

if (( ${#devices[@]} == 0 )); then
  echo "ERROR: no NVMe namespace devices detected" >&2
  exit 1
fi

if (( collection_failed != 0 )); then
  echo "ERROR: SMART collection failed for at least one NVMe device" >&2
  exit 1
fi

echo "PASS: exported SMART metrics for ${#devices[@]} NVMe devices"
