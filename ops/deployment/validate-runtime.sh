#!/usr/bin/env bash
set -Eeuo pipefail

compose_file=${1:-/opt/wishkr/current/docker-compose.ovh.yml}
env_file=${2:-/etc/wishkr/secrets/production.env}

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

for path in "${compose_file}" "${env_file}"; do
  if [[ ! -f ${path} ]]; then
    echo "ERROR: required file is missing: ${path}" >&2
    exit 1
  fi
done

secret_mode=$(stat -c '%a' "${env_file}")
if [[ ${secret_mode} != "600" ]]; then
  echo "ERROR: ${env_file} must have mode 0600 (found ${secret_mode})" >&2
  exit 1
fi

expected=(wishkr-db-1 wishkr-redis-1 wishkr-backend-1 wishkr-scheduler-1)
for container in "${expected[@]}"; do
  state=$(docker inspect --format '{{.State.Status}}' "${container}")
  health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}")
  if [[ ${state} != "running" || ${health} != "healthy" ]]; then
    echo "ERROR: ${container} state=${state} health=${health}" >&2
    exit 1
  fi
  echo "PASS: ${container} is running and healthy"
done

scheduler_count=$(
  docker ps \
    --filter label=com.docker.compose.project=wishkr \
    --filter label=com.docker.compose.service=scheduler \
    --format '{{.ID}}' |
    wc -l |
    tr -d ' '
)
if [[ ${scheduler_count} != "1" ]]; then
  echo "ERROR: expected exactly one scheduler container; found ${scheduler_count}" >&2
  exit 1
fi
echo "PASS: exactly one scheduler container is active"

if ! docker logs wishkr-scheduler-1 2>&1 |
  grep 'Scheduler advisory lock acquired' >/dev/null; then
  echo "ERROR: scheduler did not report acquisition of its PostgreSQL advisory lock" >&2
  exit 1
fi
echo "PASS: scheduler owns the PostgreSQL advisory lock"

docker exec wishkr-db-1 pg_isready -U wishkr -d wishkr >/dev/null
docker exec wishkr-db-1 sh -ec \
  'PGPASSWORD="${POSTGRES_PASSWORD}" psql -U wishkr -d wishkr -Atqc "SELECT 1"' |
  grep -qx '1'
echo "PASS: PostgreSQL accepts authenticated application-database queries"

docker exec wishkr-redis-1 sh -ec \
  'REDISCLI_AUTH="${REDIS_PASSWORD}" redis-cli ping' |
  grep -qx 'PONG'
echo "PASS: Redis accepts authenticated queries"

curl -fsS -H 'Host: api.wishkr.com' http://127.0.0.1:8000/health >/dev/null
echo "PASS: FastAPI health endpoint responds on loopback"

published=$(
  docker ps --format '{{.Names}} {{.Ports}}' |
    grep '^wishkr-' || true
)
if grep -Eq '(^|,| )0\.0\.0\.0:|(^|,| )\[::\]:' <<<"${published}"; then
  echo "ERROR: a Wishkr container publishes a port on a public wildcard" >&2
  exit 1
fi
if ! grep -Eq '^wishkr-backend-1 .*127\.0\.0\.1:8000->8000/tcp' <<<"${published}"; then
  echo "ERROR: backend loopback publication is missing or unexpected" >&2
  exit 1
fi
echo "PASS: only the backend loopback port is published"

docker compose --env-file "${env_file}" -f "${compose_file}" config --quiet
echo "PASS: runtime and Compose validation completed"
