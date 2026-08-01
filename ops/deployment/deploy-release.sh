#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASES_DIR=/opt/wishkr/releases
readonly CURRENT_LINK=/opt/wishkr/current
readonly ENV_FILE=/etc/wishkr/secrets/production.env
readonly LOCK_FILE=/run/lock/wishkr-deploy.lock

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    fail "run through the approved sudo rule"
  fi
}

preflight() {
  require_root

  command -v docker >/dev/null || fail "docker is unavailable"
  command -v flock >/dev/null || fail "flock is unavailable"
  command -v wishkr-scan-image >/dev/null || fail "wishkr-scan-image is unavailable"
  docker compose version >/dev/null || fail "docker compose is unavailable"

  [[ -d ${RELEASES_DIR} ]] || fail "release directory is missing"
  [[ -L ${CURRENT_LINK} ]] || fail "current release symlink is missing"
  [[ -f ${ENV_FILE} ]] || fail "production environment file is missing"
  [[ $(stat -c '%U:%G:%a' "${ENV_FILE}") == root:root:600 ]] \
    || fail "production environment ownership or mode is invalid"

  docker network inspect wishkr-app >/dev/null 2>&1 \
    || fail "wishkr-app network is missing"
  docker network inspect wishkr-data >/dev/null 2>&1 \
    || fail "wishkr-data network is missing"

  echo "PASS: OVH immutable-release deployment boundary is ready"
}

if [[ ${1:-} == --preflight ]]; then
  [[ $# -eq 1 ]] || fail "--preflight accepts no additional arguments"
  preflight
  exit 0
fi

require_root
[[ $# -eq 4 ]] \
  || fail "usage: wishkr-deploy-release ARCHIVE SHA256 RELEASE_ID GIT_SHA"

archive=$1
expected_sha256=$2
release_id=$3
git_sha=$4

[[ ${archive} =~ ^/var/tmp/wishkr-[0-9]+-[0-9]+\.tar\.gz$ ]] \
  || fail "archive path is outside the approved deployment namespace"
[[ -f ${archive} && ! -L ${archive} ]] || fail "release archive is not a regular file"
[[ ${expected_sha256} =~ ^[0-9a-f]{64}$ ]] || fail "invalid archive checksum"
[[ ${git_sha} =~ ^[0-9a-f]{40}$ ]] || fail "invalid Git commit identifier"
[[ ${release_id} =~ ^${git_sha}-[0-9]+-[0-9]+$ ]] || fail "invalid release identifier"

preflight

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another production deployment is active"

actual_sha256=$(sha256sum "${archive}" | awk '{print $1}')
[[ ${actual_sha256} == "${expected_sha256}" ]] || fail "release checksum mismatch"

release_dir="${RELEASES_DIR}/${release_id}"
staging_dir="${release_dir}.staging"
archive_listing=$(mktemp /var/tmp/wishkr-archive-list.XXXXXX)

cleanup() {
  rm -f "${archive}" "${archive_listing}"
  if [[ -d ${staging_dir} ]]; then
    rm -rf -- "${staging_dir}"
  fi
}
trap cleanup EXIT HUP INT TERM

[[ ! -e ${release_dir} && ! -L ${release_dir} ]] \
  || fail "release identifier already exists"
[[ ! -e ${staging_dir} && ! -L ${staging_dir} ]] \
  || fail "staging release already exists"

tar -tzf "${archive}" >"${archive_listing}"
if grep -Ev '^release(/|$)' "${archive_listing}" >/dev/null; then
  fail "archive contains entries outside the release root"
fi
if grep -E '(^|/)\.\.(/|$)|(^|/)\._' "${archive_listing}" >/dev/null; then
  fail "archive contains unsafe paths or AppleDouble metadata"
fi

install -d -o root -g root -m 0755 "${staging_dir}"
tar --extract --gzip --file="${archive}" \
  --directory="${staging_dir}" --strip-components=1 --no-same-owner
chown -R root:root "${staging_dir}"

[[ -f ${staging_dir}/docker-compose.ovh.yml ]] \
  || fail "OVH Compose definition is missing"
[[ -x ${staging_dir}/ops/deployment/validate-runtime.sh ]] \
  || fail "runtime validator is missing or not executable"
if find "${staging_dir}" -type f \( -name '.env' -o -name '*.pem' -o -name '*.key' \) \
  -print -quit | grep -q .; then
  fail "release contains a forbidden credential-shaped file"
fi

compose=(
  docker compose
  --env-file "${ENV_FILE}"
  -f "${staging_dir}/docker-compose.ovh.yml"
)

GIT_SHA="${git_sha}" "${compose[@]}" config --quiet
GIT_SHA="${git_sha}" "${compose[@]}" build db backend

wishkr-scan-image "wishkr-postgres:16.14-alpine3.24.1"
wishkr-scan-image "wishkr-backend:${git_sha}"

mv -- "${staging_dir}" "${release_dir}"
next_link="${CURRENT_LINK}.${release_id}"
ln -s "${release_dir}" "${next_link}"
mv -Tf -- "${next_link}" "${CURRENT_LINK}"

active_compose=(
  docker compose
  --env-file "${ENV_FILE}"
  -f "${CURRENT_LINK}/docker-compose.ovh.yml"
)

GIT_SHA="${git_sha}" "${active_compose[@]}" up -d --remove-orphans
"${CURRENT_LINK}/ops/deployment/validate-runtime.sh"

echo "PASS: OVH production release ${release_id} deployed and validated"
echo "Rollback remains a separate reviewed action; no automatic schema downgrade was attempted"
