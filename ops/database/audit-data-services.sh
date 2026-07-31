#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

postgres_container=${POSTGRES_CONTAINER:-wishkr-db-1}
redis_container=${REDIS_CONTAINER:-wishkr-redis-1}

pg_admin() {
  docker exec "${postgres_container}" sh -ec \
    'PGPASSWORD="${POSTGRES_ADMIN_PASSWORD}" exec psql --username=wishkr_admin --dbname=wishkr --tuples-only --no-align --field-separator="|" --command="$1"' \
    sh "$1"
}

pg_app() {
  docker exec "${postgres_container}" sh -ec \
    'PGPASSWORD="${POSTGRES_PASSWORD}" exec psql --username=wishkr --dbname=wishkr --tuples-only --no-align --field-separator="|" --command="$1"' \
    sh "$1"
}

echo "postgres_roles"
pg_admin "SELECT rolname,rolsuper,rolcreaterole,rolcreatedb,rolcanlogin,rolconnlimit FROM pg_roles WHERE rolname IN ('wishkr','wishkr_admin') ORDER BY rolname"

echo "postgres_ownership"
pg_admin "SELECT pg_get_userbyid(datdba),(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relowner=(SELECT oid FROM pg_roles WHERE rolname='wishkr_admin') AND c.relkind IN ('r','p','S','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'),(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relowner=(SELECT oid FROM pg_roles WHERE rolname='wishkr') AND c.relkind IN ('r','p','S','v','m','f') AND n.nspname NOT IN ('pg_catalog','information_schema')) FROM pg_database WHERE datname=current_database()"

echo "postgres_settings"
pg_app "SELECT name,setting,COALESCE(unit,'-'),source FROM pg_settings WHERE name IN ('effective_cache_size','hba_file','idle_in_transaction_session_timeout','log_min_duration_statement','max_connections','max_wal_size','shared_buffers','work_mem') ORDER BY name"

echo "redis_settings"
docker exec "${redis_container}" sh -ec \
  'REDISCLI_AUTH="${REDIS_PASSWORD}" redis-cli --raw CONFIG GET maxmemory maxmemory-policy appendonly appendfsync protected-mode save'

echo "redis_persistence"
docker exec "${redis_container}" sh -ec \
  'REDISCLI_AUTH="${REDIS_PASSWORD}" redis-cli --raw INFO persistence' |
  grep -E '^(aof_enabled|aof_last_bgrewrite_status|aof_last_write_status|rdb_last_bgsave_status):'

echo "protected_files"
stat -c '%U:%G|%a|%s|%n' \
  /etc/wishkr/secrets/postgres_password \
  /etc/wishkr/secrets/postgres_admin_password \
  /etc/wishkr/secrets/redis_password \
  /etc/wishkr/secrets/production.env \
  /var/backups/wishkr/item10-prechange-20260731.dump

printf 'nginx_health='
curl -ksS --resolve api.wishkr.com:443:10.77.0.1 \
  -o /dev/null -w '%{http_code}\n' https://api.wishkr.com/health
