#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"
: "${POSTGRES_ADMIN_PASSWORD:?POSTGRES_ADMIN_PASSWORD must be set}"
: "${POSTGRES_DB:?POSTGRES_DB must be set}"

if [ "${POSTGRES_USER}" != wishkr ] || [ "${POSTGRES_DB}" != wishkr ]; then
  echo "ERROR: unexpected PostgreSQL application role or database" >&2
  exit 1
fi

case ${POSTGRES_ADMIN_PASSWORD} in
  *[!0-9A-Fa-f]*|'')
    echo "ERROR: POSTGRES_ADMIN_PASSWORD must be a non-empty hexadecimal secret" >&2
    exit 1
    ;;
esac

if [ "${#POSTGRES_ADMIN_PASSWORD}" -lt 64 ]; then
  echo "ERROR: POSTGRES_ADMIN_PASSWORD must contain at least 256 bits" >&2
  exit 1
fi

export PGPASSWORD=${POSTGRES_PASSWORD}

admin_exists=$(
  psql --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
    --tuples-only --no-align \
    --command="SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='wishkr_admin')"
)

if [ "${admin_exists}" = f ]; then
  psql --set=ON_ERROR_STOP=1 --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" <<'SQL'
\set QUIET 1
\getenv admin_password POSTGRES_ADMIN_PASSWORD
BEGIN;
SELECT format(
    'CREATE ROLE wishkr_migrator WITH LOGIN SUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD %L',
    :'admin_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'wishkr_migrator'
) \gexec
SELECT format(
    'ALTER ROLE wishkr_migrator WITH LOGIN SUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD %L',
    :'admin_password'
) \gexec
COMMIT;
SQL

  export PGPASSWORD=${POSTGRES_ADMIN_PASSWORD}
  psql --set=ON_ERROR_STOP=1 --username=wishkr_migrator \
    --dbname="${POSTGRES_DB}" <<'SQL'
\set QUIET 1
\getenv admin_password POSTGRES_ADMIN_PASSWORD
\getenv app_password POSTGRES_PASSWORD
BEGIN;
ALTER ROLE wishkr RENAME TO wishkr_admin;
SELECT format(
    'ALTER ROLE wishkr_admin WITH LOGIN SUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD %L',
    :'admin_password'
) \gexec
SELECT format(
    'CREATE ROLE wishkr WITH LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 90 PASSWORD %L',
    :'app_password'
) \gexec
COMMIT;
SQL
fi

export PGPASSWORD=${POSTGRES_ADMIN_PASSWORD}
psql --set=ON_ERROR_STOP=1 --username=wishkr_admin \
  --dbname="${POSTGRES_DB}" <<'SQL'
\set QUIET 1
\getenv admin_password POSTGRES_ADMIN_PASSWORD
\getenv app_password POSTGRES_PASSWORD
BEGIN;
SELECT format(
    'ALTER ROLE wishkr_admin WITH LOGIN SUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD %L',
    :'admin_password'
) \gexec
SELECT format(
    'ALTER ROLE wishkr WITH LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 90 PASSWORD %L',
    :'app_password'
) \gexec

SELECT format('ALTER SCHEMA %I OWNER TO wishkr', n.nspname)
FROM pg_namespace n
WHERE n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER TABLE %I.%I OWNER TO wishkr', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND c.relkind IN ('r', 'p', 'f')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER SEQUENCE %I.%I OWNER TO wishkr', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND c.relkind = 'S'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER VIEW %I.%I OWNER TO wishkr', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND c.relkind = 'v'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER MATERIALIZED VIEW %I.%I OWNER TO wishkr', n.nspname, c.relname)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND c.relkind = 'm'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER TYPE %I.%I OWNER TO wishkr', n.nspname, t.typname)
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
LEFT JOIN pg_class c ON c.reltype = t.oid
WHERE t.typowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND t.typtype IN ('e', 'd', 'c', 'r', 'm')
  AND c.oid IS NULL
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER FUNCTION %I.%I(%s) OWNER TO wishkr',
              n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND p.prokind IN ('f', 'w')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER PROCEDURE %I.%I(%s) OWNER TO wishkr',
              n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND p.prokind = 'p'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

SELECT format('ALTER AGGREGATE %I.%I(%s) OWNER TO wishkr',
              n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'wishkr_admin')
  AND p.prokind = 'a'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' \gexec

ALTER DATABASE wishkr OWNER TO wishkr;
DROP ROLE IF EXISTS wishkr_migrator;
COMMIT;
SQL

unset PGPASSWORD admin_exists
echo "PASS: PostgreSQL role policy applied"
