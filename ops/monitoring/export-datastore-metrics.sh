#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

output_dir=/var/lib/alloy/textfile
output_file=${output_dir}/wishkr_datastores.prom
temporary_file=
postgres_container=wishkr-db-1
redis_container=wishkr-redis-1

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
# HELP wishkr_datastore_docker_available Whether the Docker daemon answered the datastore collection request.
# TYPE wishkr_datastore_docker_available gauge
# HELP wishkr_datastore_collection_success Whether the authenticated datastore query completed and parsed successfully.
# TYPE wishkr_datastore_collection_success gauge
# HELP wishkr_datastore_ready Whether the datastore answered its readiness check.
# TYPE wishkr_datastore_ready gauge
# HELP wishkr_postgresql_in_recovery Whether PostgreSQL reports that it is in recovery mode.
# TYPE wishkr_postgresql_in_recovery gauge
# HELP wishkr_postgresql_data_checksums_enabled Whether PostgreSQL data checksums are enabled.
# TYPE wishkr_postgresql_data_checksums_enabled gauge
# HELP wishkr_postgresql_connections Current number of PostgreSQL sessions.
# TYPE wishkr_postgresql_connections gauge
# HELP wishkr_postgresql_max_connections Configured PostgreSQL session limit.
# TYPE wishkr_postgresql_max_connections gauge
# HELP wishkr_postgresql_database_size_bytes Current size of the Wishkr PostgreSQL database in bytes.
# TYPE wishkr_postgresql_database_size_bytes gauge
# HELP wishkr_redis_loading Whether Redis is loading persisted data.
# TYPE wishkr_redis_loading gauge
# HELP wishkr_redis_rdb_changes_since_last_save Changes made since the last successful Redis RDB save.
# TYPE wishkr_redis_rdb_changes_since_last_save gauge
# HELP wishkr_redis_rdb_bgsave_in_progress Whether a Redis background RDB save is running.
# TYPE wishkr_redis_rdb_bgsave_in_progress gauge
# HELP wishkr_redis_rdb_last_save_timestamp_seconds Unix timestamp of the last successful Redis RDB save.
# TYPE wishkr_redis_rdb_last_save_timestamp_seconds gauge
# HELP wishkr_redis_rdb_last_bgsave_success Whether the last Redis background RDB save succeeded.
# TYPE wishkr_redis_rdb_last_bgsave_success gauge
# HELP wishkr_redis_aof_enabled Whether Redis append-only persistence is enabled.
# TYPE wishkr_redis_aof_enabled gauge
# HELP wishkr_redis_aof_rewrite_in_progress Whether a Redis AOF rewrite is running.
# TYPE wishkr_redis_aof_rewrite_in_progress gauge
# HELP wishkr_redis_aof_last_bgrewrite_success Whether the last Redis AOF background rewrite succeeded.
# TYPE wishkr_redis_aof_last_bgrewrite_success gauge
# HELP wishkr_redis_aof_last_write_success Whether the last Redis AOF write succeeded.
# TYPE wishkr_redis_aof_last_write_success gauge
# HELP wishkr_redis_aof_current_size_bytes Current Redis AOF size in bytes.
# TYPE wishkr_redis_aof_current_size_bytes gauge
# HELP wishkr_datastore_last_collection_timestamp_seconds Unix timestamp of the last completed datastore collection attempt.
# TYPE wishkr_datastore_last_collection_timestamp_seconds gauge
EOF

collection_failed=0
docker_available=1
if ! docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
  docker_available=0
  collection_failed=1
fi
printf 'wishkr_datastore_docker_available %d\n' "${docker_available}" >>"${temporary_file}"

postgres_ready=0
postgres_collection_success=0
postgres_values=
if (( docker_available == 1 )); then
  if docker exec "${postgres_container}" \
    pg_isready -U wishkr -d wishkr >/dev/null 2>&1; then
    postgres_ready=1
  fi

  postgres_values=$(
    docker exec "${postgres_container}" sh -ec '
      export PGPASSWORD="${POSTGRES_PASSWORD}"
      exec psql -X -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
        -At --set ON_ERROR_STOP=1 -F "|" \
        -c "SELECT CASE WHEN pg_is_in_recovery() THEN 1 ELSE 0 END, CASE current_setting('\''data_checksums'\'') WHEN '\''on'\'' THEN 1 ELSE 0 END, (SELECT count(*) FROM pg_stat_activity), current_setting('\''max_connections'\'')::integer, pg_database_size(current_database());"
    ' 2>/dev/null
  ) || postgres_values=
fi

if [[ ${postgres_values} =~ ^([01])\|([01])\|([0-9]+)\|([0-9]+)\|([0-9]+)$ ]] &&
  (( BASH_REMATCH[4] > 0 )); then
  postgres_collection_success=1
  printf 'wishkr_postgresql_in_recovery %s\n' "${BASH_REMATCH[1]}" >>"${temporary_file}"
  printf 'wishkr_postgresql_data_checksums_enabled %s\n' "${BASH_REMATCH[2]}" >>"${temporary_file}"
  printf 'wishkr_postgresql_connections %s\n' "${BASH_REMATCH[3]}" >>"${temporary_file}"
  printf 'wishkr_postgresql_max_connections %s\n' "${BASH_REMATCH[4]}" >>"${temporary_file}"
  printf 'wishkr_postgresql_database_size_bytes %s\n' "${BASH_REMATCH[5]}" >>"${temporary_file}"
else
  collection_failed=1
fi
printf 'wishkr_datastore_collection_success{datastore="postgresql"} %d\n' \
  "${postgres_collection_success}" >>"${temporary_file}"
printf 'wishkr_datastore_ready{datastore="postgresql"} %d\n' \
  "${postgres_ready}" >>"${temporary_file}"

redis_ready=0
redis_collection_success=0
redis_info=
if (( docker_available == 1 )); then
  redis_ping=$(
    docker exec "${redis_container}" sh -ec \
      'export REDISCLI_AUTH="${REDIS_PASSWORD}"; exec redis-cli --raw PING' \
      2>/dev/null
  ) || redis_ping=
  if [[ ${redis_ping} == PONG ]]; then
    redis_ready=1
  fi

  redis_info=$(
    docker exec "${redis_container}" sh -ec \
      'export REDISCLI_AUTH="${REDIS_PASSWORD}"; exec redis-cli --raw INFO persistence' \
      2>/dev/null | tr -d '\r'
  ) || redis_info=
fi

redis_field() {
  local field_name=$1
  awk -F: -v field_name="${field_name}" '$1 == field_name {print $2; exit}' \
    <<<"${redis_info}"
}

redis_loading=$(redis_field loading)
redis_rdb_changes=$(redis_field rdb_changes_since_last_save)
redis_rdb_in_progress=$(redis_field rdb_bgsave_in_progress)
redis_rdb_last_save=$(redis_field rdb_last_save_time)
redis_rdb_status=$(redis_field rdb_last_bgsave_status)
redis_aof_enabled=$(redis_field aof_enabled)
redis_aof_in_progress=$(redis_field aof_rewrite_in_progress)
redis_aof_rewrite_status=$(redis_field aof_last_bgrewrite_status)
redis_aof_write_status=$(redis_field aof_last_write_status)
redis_aof_size=$(redis_field aof_current_size)

if [[ ${redis_loading} =~ ^[01]$ &&
  ${redis_rdb_changes} =~ ^[0-9]+$ &&
  ${redis_rdb_in_progress} =~ ^[01]$ &&
  ${redis_rdb_last_save} =~ ^[0-9]+$ &&
  ${redis_rdb_status} =~ ^(ok|err)$ &&
  ${redis_aof_enabled} =~ ^[01]$ &&
  ${redis_aof_in_progress} =~ ^[01]$ &&
  ${redis_aof_rewrite_status} =~ ^(ok|err)$ &&
  ${redis_aof_write_status} =~ ^(ok|err)$ &&
  ${redis_aof_size} =~ ^[0-9]+$ ]]; then
  redis_collection_success=1
  [[ ${redis_rdb_status} == ok ]] && redis_rdb_success=1 || redis_rdb_success=0
  [[ ${redis_aof_rewrite_status} == ok ]] && redis_aof_rewrite_success=1 || redis_aof_rewrite_success=0
  [[ ${redis_aof_write_status} == ok ]] && redis_aof_write_success=1 || redis_aof_write_success=0

  printf 'wishkr_redis_loading %s\n' "${redis_loading}" >>"${temporary_file}"
  printf 'wishkr_redis_rdb_changes_since_last_save %s\n' "${redis_rdb_changes}" >>"${temporary_file}"
  printf 'wishkr_redis_rdb_bgsave_in_progress %s\n' "${redis_rdb_in_progress}" >>"${temporary_file}"
  printf 'wishkr_redis_rdb_last_save_timestamp_seconds %s\n' "${redis_rdb_last_save}" >>"${temporary_file}"
  printf 'wishkr_redis_rdb_last_bgsave_success %d\n' "${redis_rdb_success}" >>"${temporary_file}"
  printf 'wishkr_redis_aof_enabled %s\n' "${redis_aof_enabled}" >>"${temporary_file}"
  printf 'wishkr_redis_aof_rewrite_in_progress %s\n' "${redis_aof_in_progress}" >>"${temporary_file}"
  printf 'wishkr_redis_aof_last_bgrewrite_success %d\n' "${redis_aof_rewrite_success}" >>"${temporary_file}"
  printf 'wishkr_redis_aof_last_write_success %d\n' "${redis_aof_write_success}" >>"${temporary_file}"
  printf 'wishkr_redis_aof_current_size_bytes %s\n' "${redis_aof_size}" >>"${temporary_file}"
else
  collection_failed=1
fi
printf 'wishkr_datastore_collection_success{datastore="redis"} %d\n' \
  "${redis_collection_success}" >>"${temporary_file}"
printf 'wishkr_datastore_ready{datastore="redis"} %d\n' \
  "${redis_ready}" >>"${temporary_file}"

printf 'wishkr_datastore_last_collection_timestamp_seconds %(%s)T\n' -1 \
  >>"${temporary_file}"

chown root:alloy "${temporary_file}"
chmod 0640 "${temporary_file}"
mv -f -- "${temporary_file}" "${output_file}"
temporary_file=

if (( collection_failed != 0 )); then
  echo "ERROR: PostgreSQL or Redis metric collection failed" >&2
  exit 1
fi

echo "PASS: exported authenticated PostgreSQL and Redis health metrics"
