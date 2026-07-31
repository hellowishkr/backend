#!/usr/bin/env bash
set -Eeuo pipefail

umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

destination=${1:-r2}
if [[ ${destination} != r2 && ${destination} != ovh ]]; then
  echo "Usage: $0 [r2|ovh]" >&2
  exit 1
fi

r2_credentials_file=${WISHKR_R2_CREDENTIALS_FILE:-/etc/wishkr/secrets/r2-backup.env}
ovh_credentials_file=${WISHKR_OVH_CREDENTIALS_FILE:-/etc/wishkr/secrets/ovh-backup.env}
postgres_container=${POSTGRES_CONTAINER:-wishkr-db-1}
redis_container=${REDIS_CONTAINER:-wishkr-redis-1}
restore_root=${WISHKR_RESTORE_TEST_DIR:-/var/backups/wishkr/restore-tests}
cache_dir=${RESTIC_CACHE_DIR:-/var/cache/wishkr-restic}

credentials_file=${r2_credentials_file}
if [[ ${destination} == ovh ]]; then
  credentials_file=${ovh_credentials_file}
fi

if [[ ! -s ${credentials_file} ]]; then
  echo "ERROR: missing ${destination} credentials file" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${credentials_file}"
set +a
export RESTIC_CACHE_DIR=${cache_dir}

install -d -o root -g root -m 0700 "${restore_root}" "${cache_dir}"
restore_dir=$(mktemp -d "${restore_root}/drill.XXXXXX")
restore_target=${restore_dir}/restored
repository_copy=${restore_dir}/repository
scratch_db="wishkr_restore_test_$(date -u +%Y%m%d%H%M%S)_$$"
scratch_created=false

cleanup() {
  if [[ ${scratch_created} == true && ${scratch_db} =~ ^wishkr_restore_test_[0-9]{14}_[0-9]+$ ]]; then
    docker exec "${postgres_container}" sh -ec \
      'PGPASSWORD="${POSTGRES_ADMIN_PASSWORD}" exec dropdb --username=wishkr_admin --if-exists "$1"' \
      sh "${scratch_db}" >/dev/null 2>&1 || true
  fi
  if [[ -n ${restore_dir:-} && -d ${restore_dir} ]]; then
    rm -rf -- "${restore_dir}"
  fi
}
trap cleanup EXIT

if [[ ${destination} == ovh ]]; then
  if [[ -z ${OVH_RCLONE_DESTINATION:-} || -z ${OVH_RESTIC_PASSWORD_FILE:-} ]]; then
    echo "ERROR: incomplete OVH repository configuration" >&2
    exit 1
  fi
  install -d -o root -g root -m 0700 "${repository_copy}"
  echo "restore_phase=ovh_ftps_download status=started"
  rclone copy "${OVH_RCLONE_DESTINATION}" "${repository_copy}" \
    --checkers 1 \
    --transfers 1 \
    --retries 3 \
    --low-level-retries 10 \
    --quiet
  export RESTIC_REPOSITORY=${repository_copy}
  export RESTIC_PASSWORD_FILE=${OVH_RESTIC_PASSWORD_FILE}
  echo "restore_phase=ovh_ftps_download status=passed"
fi

echo "restore_phase=${destination}_repository_check status=started"
restic check --read-data --quiet
echo "restore_phase=${destination}_repository_check status=passed"

echo "restore_phase=snapshot_download status=started"
restic restore latest --target "${restore_target}" --quiet
postgres_dump=$(find "${restore_target}" -type f -name wishkr-postgres.dump -print -quit)
redis_dump=$(find "${restore_target}" -type f -name wishkr-redis.rdb -print -quit)
manifest=$(find "${restore_target}" -type f -name manifest.txt -print -quit)

for restored_file in "${postgres_dump}" "${redis_dump}" "${manifest}"; do
  if [[ -z ${restored_file} || ! -s ${restored_file} ]]; then
    echo "ERROR: required restored artifact is missing" >&2
    exit 1
  fi
done

expected_postgres_sha=$(awk -F= '$1 == "postgres_sha256" {print $2}' "${manifest}")
expected_redis_sha=$(awk -F= '$1 == "redis_sha256" {print $2}' "${manifest}")
actual_postgres_sha=$(sha256sum "${postgres_dump}" | awk '{print $1}')
actual_redis_sha=$(sha256sum "${redis_dump}" | awk '{print $1}')

if [[ ${actual_postgres_sha} != "${expected_postgres_sha}" || ${actual_redis_sha} != "${expected_redis_sha}" ]]; then
  echo "ERROR: restored artifact checksum mismatch" >&2
  exit 1
fi
echo "restore_phase=snapshot_download status=passed checksums=passed"

redis_image=$(docker inspect --format '{{.Config.Image}}' "${redis_container}")
echo "restore_phase=redis_validation status=started"
docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "${redis_dump}:/restore/dump.rdb:ro" \
  --entrypoint redis-check-rdb \
  "${redis_image}" /restore/dump.rdb >/dev/null
echo "restore_phase=redis_validation status=passed"

echo "restore_phase=postgres_restore status=started"
docker exec "${postgres_container}" sh -ec \
  'PGPASSWORD="${POSTGRES_ADMIN_PASSWORD}" exec createdb --username=wishkr_admin --owner=wishkr "$1"' \
  sh "${scratch_db}" >/dev/null
scratch_created=true

docker exec -i "${postgres_container}" sh -ec \
  'PGPASSWORD="${POSTGRES_PASSWORD}" exec pg_restore \
    --username="${POSTGRES_USER:-wishkr}" \
    --dbname="$1" \
    --no-owner --no-privileges --exit-on-error' \
  sh "${scratch_db}" <"${postgres_dump}" >/dev/null

sql="SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';"
source_table_count=$(docker exec "${postgres_container}" sh -ec \
  'PGPASSWORD="${POSTGRES_PASSWORD}" exec psql \
    --username="${POSTGRES_USER:-wishkr}" --dbname="${POSTGRES_DB:-wishkr}" \
    --tuples-only --no-align --command="$1"' sh "${sql}")
restored_table_count=$(docker exec "${postgres_container}" sh -ec \
  'PGPASSWORD="${POSTGRES_PASSWORD}" exec psql \
    --username="${POSTGRES_USER:-wishkr}" --dbname="$1" \
    --tuples-only --no-align --command="$2"' sh "${scratch_db}" "${sql}")

if [[ ! ${source_table_count} =~ ^[0-9]+$ || ${source_table_count} -eq 0 ]]; then
  echo "ERROR: source database table count is invalid" >&2
  exit 1
fi

if [[ ${restored_table_count} != "${source_table_count}" ]]; then
  echo "ERROR: restored table count does not match source" >&2
  exit 1
fi

echo "restore_phase=postgres_restore status=passed table_count=${restored_table_count}"
echo "restore_result=success destination=${destination}"
