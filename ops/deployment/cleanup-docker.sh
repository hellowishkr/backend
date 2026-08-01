#!/usr/bin/env bash
set -Eeuo pipefail

readonly LOCK_FILE=/run/lock/wishkr-deploy.lock
readonly MAX_AGE=168h
readonly CURRENT_LINK=/opt/wishkr/current

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ ${EUID} -ne 0 ]]; then
  fail "run through the approved sudo rule"
fi
if [[ $# -ne 0 ]]; then
  fail "this helper accepts no arguments"
fi

command -v docker >/dev/null || fail "docker is unavailable"
command -v flock >/dev/null || fail "flock is unavailable"
[[ -L ${CURRENT_LINK} ]] || fail "current release symlink is missing"
[[ -x ${CURRENT_LINK}/ops/deployment/validate-runtime.sh ]] \
  || fail "runtime validator is missing"

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another production deployment or maintenance action is active"

"${CURRENT_LINK}/ops/deployment/validate-runtime.sh" >/dev/null

running_before=$(docker ps --quiet | wc -l)
stopped_before=$(docker ps --all --quiet --filter status=created \
  --filter status=exited --filter status=dead | wc -l)

echo "cleanup_scope=stopped_containers max_age=${MAX_AGE} candidates_before=${stopped_before}"
docker container prune --force --filter "until=${MAX_AGE}"

running_after=$(docker ps --quiet | wc -l)
[[ ${running_after} -eq ${running_before} ]] \
  || fail "running container count changed during cleanup"

"${CURRENT_LINK}/ops/deployment/validate-runtime.sh" >/dev/null
echo "PASS: stale stopped-container cleanup completed; running services were unchanged"
echo "Images, volumes, networks, build cache, and rollback artifacts were not pruned"
