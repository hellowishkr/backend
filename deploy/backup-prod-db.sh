#!/usr/bin/env bash
# Manual/cron pg_dump backup for Wishkr production DB.
# Usage: APP_DIR=/opt/wishkr deploy/backup-prod-db.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/wishkr}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_CONTAINER="${DB_CONTAINER:-wishkr-db-1}"
POSTGRES_USER="${POSTGRES_USER:-wishkr}"
POSTGRES_DB="${POSTGRES_DB:-wishkr}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/pre_3server_migration_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

if ! docker inspect "$DB_CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: DB container $DB_CONTAINER not found" >&2
  exit 1
fi

echo "Creating backup: $BACKUP_FILE"
docker exec "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$BACKUP_FILE"

test -s "$BACKUP_FILE"
gzip -t "$BACKUP_FILE"
sha256sum "$BACKUP_FILE" > "$BACKUP_FILE.sha256"

find "$BACKUP_DIR" -type f \( -name '*.sql.gz' -o -name '*.sql.gz.sha256' \) -mtime +"$RETENTION_DAYS" -print -delete

# Off-site copy to Cloudflare R2 (rclone remote named "r2", bucket in
# R2_BUCKET, default wishkr-db-backup). Skipped silently when rclone or the
# remote isn't configured, so the local backup still succeeds standalone.
# Upload failure must NOT fail the local backup — it's an extra copy.
R2_BUCKET="${R2_BUCKET:-wishkr-db-backup}"
R2_RETENTION_DAYS="${R2_RETENTION_DAYS:-30}"
if command -v rclone >/dev/null 2>&1 && rclone listremotes 2>/dev/null | grep -q '^r2:'; then
  if rclone copy "$BACKUP_FILE" "r2:$R2_BUCKET/db/" --s3-no-check-bucket \
     && rclone copy "$BACKUP_FILE.sha256" "r2:$R2_BUCKET/db/" --s3-no-check-bucket; then
    echo "r2_upload=ok"
    # Prune remote copies older than R2_RETENTION_DAYS.
    rclone delete "r2:$R2_BUCKET/db/" --min-age "${R2_RETENTION_DAYS}d" 2>/dev/null || true
  else
    echo "r2_upload=FAILED — local backup OK, but off-site copy did not happen" >&2
  fi
else
  echo "r2_upload=skipped (rclone remote 'r2' not configured)"
fi

printf 'backup_file=%s\n' "$BACKUP_FILE"
printf 'backup_size_bytes=%s\n' "$(stat -c %s "$BACKUP_FILE" 2>/dev/null || stat -f %z "$BACKUP_FILE")"
printf 'sha256=' && cat "$BACKUP_FILE.sha256"
echo "gzip_test=passed"
