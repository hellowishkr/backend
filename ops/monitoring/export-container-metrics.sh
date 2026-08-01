#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

output_dir=/var/lib/alloy/textfile
output_file=${output_dir}/wishkr_containers.prom
temporary_file=
containers=(wishkr-db-1 wishkr-redis-1 wishkr-backend-1 wishkr-scheduler-1)

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
# HELP wishkr_container_expected Number of containers expected by the production deployment.
# TYPE wishkr_container_expected gauge
# HELP wishkr_container_collection_success Whether Docker state collection succeeded for the container.
# TYPE wishkr_container_collection_success gauge
# HELP wishkr_container_docker_available Whether the Docker daemon answered the collection request.
# TYPE wishkr_container_docker_available gauge
# HELP wishkr_container_present Whether the expected container exists.
# TYPE wishkr_container_present gauge
# HELP wishkr_container_running Whether the container is running.
# TYPE wishkr_container_running gauge
# HELP wishkr_container_healthy Whether the container's configured health check reports healthy.
# TYPE wishkr_container_healthy gauge
# HELP wishkr_container_restarting Whether Docker reports that the container is restarting.
# TYPE wishkr_container_restarting gauge
# HELP wishkr_container_oom_killed Whether Docker reports that the container was OOM-killed.
# TYPE wishkr_container_oom_killed gauge
# HELP wishkr_container_restart_count_total Docker restart count for the container.
# TYPE wishkr_container_restart_count_total counter
# HELP wishkr_container_exit_code Last container process exit code reported by Docker.
# TYPE wishkr_container_exit_code gauge
# HELP wishkr_container_started_at_timestamp_seconds Unix timestamp at which the current container process started.
# TYPE wishkr_container_started_at_timestamp_seconds gauge
# HELP wishkr_container_last_collection_timestamp_seconds Unix timestamp of the last completed collection attempt.
# TYPE wishkr_container_last_collection_timestamp_seconds gauge
EOF

printf 'wishkr_container_expected %d\n' "${#containers[@]}" >>"${temporary_file}"
collection_failed=0
docker_available=1

if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  docker_available=0
  collection_failed=1
fi
printf 'wishkr_container_docker_available %d\n' "${docker_available}" >>"${temporary_file}"

for container in "${containers[@]}"; do
  if (( docker_available == 0 )); then
    printf 'wishkr_container_collection_success{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_present{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_running{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_healthy{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_restarting{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_oom_killed{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_restart_count_total{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_exit_code{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_started_at_timestamp_seconds{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    continue
  fi

  if ! inspect_json=$(docker container inspect "${container}" 2>/dev/null); then
    printf 'wishkr_container_collection_success{container="%s"} 1\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_present{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_running{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_healthy{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_restarting{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_oom_killed{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_restart_count_total{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_exit_code{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    printf 'wishkr_container_started_at_timestamp_seconds{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    continue
  fi

  values=$(jq --exit-status --raw-output '
    .[0] |
    [
      (if .State.Running == true then 1 else 0 end),
      (if .State.Health.Status == "healthy" then 1 else 0 end),
      (if .State.Restarting == true then 1 else 0 end),
      (if .State.OOMKilled == true then 1 else 0 end),
      (.RestartCount | numbers),
      (.State.ExitCode | numbers),
      (.State.StartedAt | strings)
    ] | @tsv
  ' <<<"${inspect_json}" 2>/dev/null) || values=

  if [[ -z ${values} ]]; then
    printf 'wishkr_container_collection_success{container="%s"} 0\n' "${container}" >>"${temporary_file}"
    collection_failed=1
    continue
  fi

  IFS=$'\t' read -r \
    running healthy restarting oom_killed restart_count exit_code started_at \
    <<<"${values}"

  started_at_timestamp=0
  if [[ ${started_at} != 0001-01-01T00:00:00Z ]]; then
    started_at_timestamp=$(date --date="${started_at}" +%s 2>/dev/null) || started_at_timestamp=0
  fi

  printf 'wishkr_container_collection_success{container="%s"} 1\n' "${container}" >>"${temporary_file}"
  printf 'wishkr_container_present{container="%s"} 1\n' "${container}" >>"${temporary_file}"
  printf 'wishkr_container_running{container="%s"} %s\n' "${container}" "${running}" >>"${temporary_file}"
  printf 'wishkr_container_healthy{container="%s"} %s\n' "${container}" "${healthy}" >>"${temporary_file}"
  printf 'wishkr_container_restarting{container="%s"} %s\n' "${container}" "${restarting}" >>"${temporary_file}"
  printf 'wishkr_container_oom_killed{container="%s"} %s\n' "${container}" "${oom_killed}" >>"${temporary_file}"
  printf 'wishkr_container_restart_count_total{container="%s"} %s\n' "${container}" "${restart_count}" >>"${temporary_file}"
  printf 'wishkr_container_exit_code{container="%s"} %s\n' "${container}" "${exit_code}" >>"${temporary_file}"
  printf 'wishkr_container_started_at_timestamp_seconds{container="%s"} %s\n' "${container}" "${started_at_timestamp}" >>"${temporary_file}"
done

printf 'wishkr_container_last_collection_timestamp_seconds %(%s)T\n' -1 >>"${temporary_file}"

chown root:alloy "${temporary_file}"
chmod 0640 "${temporary_file}"
mv -f -- "${temporary_file}" "${output_file}"
temporary_file=

if (( collection_failed != 0 )); then
  echo "ERROR: Docker state collection failed" >&2
  exit 1
fi

echo "PASS: exported state for ${#containers[@]} expected containers"
