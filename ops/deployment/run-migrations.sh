#!/usr/bin/env bash
set -Eeuo pipefail

readonly LOCK_FILE=/run/lock/wishkr-deploy.lock
readonly CURRENT_LINK=/opt/wishkr/current
readonly ENV_FILE=/etc/wishkr/secrets/production.env

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ ${EUID} -ne 0 ]]; then
  fail "run through the approved sudo rule"
fi
if [[ $# -ne 1 ]]; then
  fail "usage: wishkr-run-migrations EXPECTED_GIT_SHA"
fi

expected_git_sha=$1
[[ ${expected_git_sha} =~ ^[0-9a-f]{40}$ ]] \
  || fail "invalid expected Git commit identifier"

for command_name in docker flock wishkr-backup wishkr-backup-status; do
  command -v "${command_name}" >/dev/null \
    || fail "required command is unavailable: ${command_name}"
done
docker compose version >/dev/null || fail "docker compose is unavailable"
[[ -L ${CURRENT_LINK} ]] || fail "current release symlink is missing"
[[ -f ${ENV_FILE} ]] || fail "production environment file is missing"
[[ -x ${CURRENT_LINK}/ops/deployment/validate-runtime.sh ]] \
  || fail "runtime validator is missing"

compose=(
  docker compose
  --env-file "${ENV_FILE}"
  -f "${CURRENT_LINK}/docker-compose.ovh.yml"
)

backend_id=$("${compose[@]}" ps --quiet backend)
[[ -n ${backend_id} ]] || fail "backend container is unavailable"
active_git_sha=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
  "${backend_id}" | grep '^GIT_SHA=' | cut -d= -f2-)
[[ ${active_git_sha} == "${expected_git_sha}" ]] \
  || fail "active OVH release does not match the selected Git commit; deploy it first"

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another production deployment or maintenance action is active"

"${CURRENT_LINK}/ops/deployment/validate-runtime.sh" >/dev/null

echo "migration_phase=pre_migration_backup status=started"
wishkr-backup
wishkr-backup-status >/dev/null
echo "migration_phase=pre_migration_backup status=passed"

echo "migration_phase=alembic_upgrade status=started"
"${compose[@]}" exec -T backend alembic upgrade head
echo "migration_phase=alembic_upgrade status=passed"

"${compose[@]}" exec -T backend python - <<'PY'
import asyncio
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> None:
    expected = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            current = set(result.scalars())
    finally:
        await engine.dispose()
    if current != expected:
        raise SystemExit("database migration heads do not match the active release")


asyncio.run(main())
PY

"${CURRENT_LINK}/ops/deployment/validate-runtime.sh" >/dev/null
echo "PASS: active-release database migrations and runtime validation completed"
