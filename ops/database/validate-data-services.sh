#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

postgres_container=${POSTGRES_CONTAINER:-wishkr-db-1}
redis_container=${REDIS_CONTAINER:-wishkr-redis-1}

for container in "${postgres_container}" "${redis_container}"; do
  state=$(docker inspect --format '{{.State.Status}}' "${container}")
  health=$(docker inspect --format '{{.State.Health.Status}}' "${container}")
  if [[ ${state} != running || ${health} != healthy ]]; then
    echo "ERROR: ${container} state=${state} health=${health}" >&2
    exit 1
  fi
done

postgres_settings=$(
  docker exec "${postgres_container}" sh -ec \
    'PGPASSWORD="${POSTGRES_PASSWORD}" exec psql --username=wishkr --dbname=wishkr --tuples-only --no-align --field-separator="|" --command="$1"' \
    sh "SELECT name, setting FROM pg_settings WHERE name IN ('effective_cache_size','log_min_duration_statement','max_connections','shared_buffers','work_mem') ORDER BY name"
)

for expected in \
  'effective_cache_size|1572864' \
  'log_min_duration_statement|1000' \
  'max_connections|100' \
  'shared_buffers|524288' \
  'work_mem|8192'; do
  if ! grep -qx "${expected}" <<<"${postgres_settings}"; then
    echo "ERROR: missing PostgreSQL setting ${expected}" >&2
    exit 1
  fi
done

app_role=$(
  docker exec "${postgres_container}" sh -ec \
    'PGPASSWORD="${POSTGRES_PASSWORD}" exec psql --username=wishkr --dbname=wishkr --tuples-only --no-align --field-separator="|" --command="$1"' \
    sh "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname='wishkr'"
)
if [[ ${app_role} != 'f|f|f' ]]; then
  echo "ERROR: PostgreSQL application role remains privileged" >&2
  exit 1
fi

if docker exec "${postgres_container}" \
  psql --username=wishkr --dbname=wishkr --tuples-only --no-align \
  --command='SELECT 1' >/dev/null 2>&1; then
  echo "ERROR: passwordless local PostgreSQL authentication succeeded" >&2
  exit 1
fi

if docker exec "${postgres_container}" sh -ec \
  'PGPASSWORD="${POSTGRES_PASSWORD}" psql --username=wishkr --dbname=wishkr --set=ON_ERROR_STOP=1 --command="BEGIN; CREATE ROLE wishkr_privilege_probe; ROLLBACK"' \
  >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL application role can create roles" >&2
  exit 1
fi

redis_config=$(
  docker exec "${redis_container}" sh -ec \
    'REDISCLI_AUTH="${REDIS_PASSWORD}" redis-cli --raw CONFIG GET maxmemory maxmemory-policy appendonly appendfsync protected-mode'
)
for expected in 1073741824 allkeys-lru yes everysec; do
  if ! grep -qx "${expected}" <<<"${redis_config}"; then
    echo "ERROR: missing Redis setting ${expected}" >&2
    exit 1
  fi
done

if ! docker exec "${redis_container}" sh -ec \
  'REDISCLI_AUTH="${REDIS_PASSWORD}" redis-cli ping' | grep -qx PONG; then
  echo "ERROR: authenticated Redis PING failed" >&2
  exit 1
fi

published=$(docker ps --format '{{.Names}}|{{.Ports}}')
if grep -E '^wishkr-(db|redis)-1\|.*(0\.0\.0\.0|\[::\]):' <<<"${published}" >/dev/null; then
  echo "ERROR: PostgreSQL or Redis publishes a host port" >&2
  exit 1
fi

echo "PASS: PostgreSQL and Redis hardening validation completed"
