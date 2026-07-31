#!/usr/bin/env bash
set -Eeuo pipefail

POSTGRES_IMAGE="${1:-wishkr-postgres:16.14-alpine3.24.1}"
REDIS_IMAGE="${2:-redis:7-alpine@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2}"

TEST_NETWORK="wishkr-item7-data-test"
POSTGRES_VOLUME="wishkr-item7-pg-test"
REDIS_VOLUME="wishkr-item7-redis-test"
POSTGRES_CONTAINER="wishkr-item7-postgres-test"
REDIS_CONTAINER="wishkr-item7-redis-test"
TEST_PASSWORD="item7-disposable-password"

if (( EUID != 0 )); then
  echo "Run this test as root." >&2
  exit 1
fi

for object in \
  "${TEST_NETWORK}" \
  "${POSTGRES_VOLUME}" \
  "${REDIS_VOLUME}" \
  "${POSTGRES_CONTAINER}" \
  "${REDIS_CONTAINER}"; do
  if docker network inspect "${object}" >/dev/null 2>&1 ||
     docker volume inspect "${object}" >/dev/null 2>&1 ||
     docker container inspect "${object}" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing Docker object: ${object}" >&2
    exit 1
  fi
done

cleanup() {
  docker container rm --force "${POSTGRES_CONTAINER}" "${REDIS_CONTAINER}" \
    >/dev/null 2>&1 || true
  docker volume rm "${POSTGRES_VOLUME}" "${REDIS_VOLUME}" >/dev/null 2>&1 || true
  docker network rm "${TEST_NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create \
  --internal \
  --subnet 172.28.250.0/24 \
  --opt com.docker.network.bridge.name=br-wishkr-it7 \
  "${TEST_NETWORK}" >/dev/null
docker volume create "${POSTGRES_VOLUME}" >/dev/null
docker volume create "${REDIS_VOLUME}" >/dev/null

docker run --detach \
  --name "${POSTGRES_CONTAINER}" \
  --network "${TEST_NETWORK}" \
  --user 70:70 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 512 \
  --memory 2g \
  --cpus 2 \
  --shm-size 256m \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --tmpfs /var/run/postgresql:rw,nosuid,nodev,uid=70,gid=70,mode=3775,size=64m \
  --mount "type=volume,source=${POSTGRES_VOLUME},target=/var/lib/postgresql/data" \
  --env POSTGRES_USER=wishkr \
  --env "POSTGRES_PASSWORD=${TEST_PASSWORD}" \
  --env POSTGRES_DB=wishkr \
  "${POSTGRES_IMAGE}" >/dev/null

docker run --detach \
  --name "${REDIS_CONTAINER}" \
  --network "${TEST_NETWORK}" \
  --user 999:1000 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 128 \
  --memory 512m \
  --cpus 1 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --mount "type=volume,source=${REDIS_VOLUME},target=/data" \
  "${REDIS_IMAGE}" \
  redis-server --appendonly yes --requirepass "${TEST_PASSWORD}" >/dev/null

for _ in {1..30}; do
  if docker exec "${POSTGRES_CONTAINER}" pg_isready -U wishkr -d wishkr \
       >/dev/null 2>&1 &&
     docker exec "${REDIS_CONTAINER}" redis-cli --no-auth-warning \
       -a "${TEST_PASSWORD}" ping 2>/dev/null | grep -qx PONG; then
    break
  fi
  sleep 1
done

docker exec "${POSTGRES_CONTAINER}" pg_isready -U wishkr -d wishkr
docker exec --env "PGPASSWORD=${TEST_PASSWORD}" "${POSTGRES_CONTAINER}" \
  psql -U wishkr -d wishkr -v ON_ERROR_STOP=1 \
  -c "SELECT current_user, current_database();"
docker exec "${REDIS_CONTAINER}" redis-cli --no-auth-warning \
  -a "${TEST_PASSWORD}" SET item7 healthy >/dev/null
docker exec "${REDIS_CONTAINER}" redis-cli --no-auth-warning \
  -a "${TEST_PASSWORD}" GET item7 | grep -qx healthy

for container in "${POSTGRES_CONTAINER}" "${REDIS_CONTAINER}"; do
  docker inspect "${container}" --format '{{.Config.User}}' | grep -Evqx '0|0:0|root'
  docker inspect "${container}" --format '{{.HostConfig.ReadonlyRootfs}}' | grep -qx true
  docker inspect "${container}" --format '{{json .HostConfig.CapDrop}}' | grep -q '"ALL"'
  docker inspect "${container}" --format '{{json .HostConfig.SecurityOpt}}' |
    grep -q 'no-new-privileges'
done

docker network inspect "${TEST_NETWORK}" --format '{{.Internal}}' | grep -qx true

echo "PASS: PostgreSQL and Redis run healthy with non-root users and hardened isolation"
