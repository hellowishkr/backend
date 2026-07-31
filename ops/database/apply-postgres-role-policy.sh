#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

container=${POSTGRES_CONTAINER:-wishkr-db-1}

if [[ $(docker inspect --format '{{.State.Health.Status}}' "${container}") != healthy ]]; then
  echo "ERROR: ${container} is not healthy" >&2
  exit 1
fi

docker exec "${container}" /docker-entrypoint-initdb.d/001-wishkr-role-policy.sh

role_state=$(
  docker exec "${container}" sh -ec \
    'PGPASSWORD="${POSTGRES_ADMIN_PASSWORD}" psql --username=wishkr_admin --dbname=wishkr --tuples-only --no-align --command="SELECT rolname || '\''|'\'' || rolsuper || '\''|'\'' || rolcreaterole || '\''|'\'' || rolcreatedb || '\''|'\'' || rolcanlogin || '\''|'\'' || rolconnlimit FROM pg_roles WHERE rolname IN ('\''wishkr'\'', '\''wishkr_admin'\'') ORDER BY rolname"'
)

expected=$'wishkr|false|false|false|true|90\nwishkr_admin|true|false|false|true|3'
if [[ ${role_state} != "${expected}" ]]; then
  echo "ERROR: PostgreSQL role policy verification failed" >&2
  exit 1
fi

echo "PASS: PostgreSQL application and local-administrator roles are separated"
