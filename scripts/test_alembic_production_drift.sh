#!/usr/bin/env bash
# Simulate the production Alembic drift observed on 2026-05-10:
# - alembic_version says a1b3c2d4e5f6
# - wishlist/preference tables from d9/e1 are missing
# - purchase branch objects already exist
# Then verify alembic upgrade head succeeds and reconciles the DB.
set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-wishkr-backend-db-1}"
DB_USER="${POSTGRES_USER:-wishkr}"
DB_PASSWORD="${POSTGRES_PASSWORD:-wishkr_dev}"
DB_NAME="wishkr_drift_repair_test_$(date +%Y%m%d%H%M%S)"
DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"

docker exec "$CONTAINER" createdb -U "$DB_USER" "$DB_NAME"
trap 'docker exec "$CONTAINER" dropdb -U "$DB_USER" --if-exists "$DB_NAME" >/dev/null 2>&1 || true' EXIT

DATABASE_URL="$DATABASE_URL" alembic upgrade b2e5f9c8a1d3

docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 <<'SQL'
-- Simulate the inconsistent alembic marker.
UPDATE alembic_version SET version_num = 'a1b3c2d4e5f6';

-- Simulate a1 order/platform fields.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gift_amount NUMERIC(10, 2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS platform_fee NUMERIC(10, 2);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS platform_fee_gst NUMERIC(10, 2);

-- Simulate purchase branch objects that production already had.
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS wishlist_item_id VARCHAR(30);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_purchases_wishlist_item') THEN
    ALTER TABLE purchases ADD CONSTRAINT fk_purchases_wishlist_item
      FOREIGN KEY (wishlist_item_id) REFERENCES wishlist_items(id) ON DELETE SET NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_purchases_wishlist_item
  ON purchases (wishlist_item_id) WHERE wishlist_item_id IS NOT NULL;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS razorpay_short_url VARCHAR(255);
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reminders ALTER COLUMN attempt_count DROP DEFAULT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS last_error VARCHAR(500);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE failed_outbound_messages ADD COLUMN IF NOT EXISTS template_name VARCHAR(100);
ALTER TABLE failed_outbound_messages ADD COLUMN IF NOT EXISTS template_params_json TEXT;

CREATE TABLE IF NOT EXISTS honeypot_hits (
  id VARCHAR(30) PRIMARY KEY,
  endpoint VARCHAR(60) NOT NULL,
  ip VARCHAR(45),
  user_agent VARCHAR(500),
  email VARCHAR(255),
  brand_name VARCHAR(120),
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_honeypot_hits_endpoint ON honeypot_hits (endpoint);
CREATE INDEX IF NOT EXISTS ix_honeypot_hits_created_at ON honeypot_hits (created_at);

ALTER TYPE gifttype ADD VALUE IF NOT EXISTS 'EXPERIENCE';
ALTER TYPE gifttype ADD VALUE IF NOT EXISTS 'PRIVATE_THEATRE';
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS provider VARCHAR(40);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS provider_product_id VARCHAR(80);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS provider_order_id VARCHAR(80);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS fulfillment_policy VARCHAR(40);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS fulfillment_lead_time_hours INTEGER;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS fulfillment_due_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS fulfillment_metadata_json JSONB;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS recipient_user_id VARCHAR(30);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS recipient_phone VARCHAR(15);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS delivery_address_id VARCHAR(30);
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS special_instructions TEXT;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_purchases_recipient_user_id_users') THEN
    ALTER TABLE purchases ADD CONSTRAINT fk_purchases_recipient_user_id_users
      FOREIGN KEY (recipient_user_id) REFERENCES users(id) ON DELETE SET NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_purchases_provider_due ON purchases (provider, fulfillment_due_at);
CREATE INDEX IF NOT EXISTS ix_purchases_recipient_user ON purchases (recipient_user_id);
SQL

DATABASE_URL="$DATABASE_URL" alembic upgrade head

version=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT version_num FROM alembic_version")
[ "$version" = "95b02584a118" ]

for table in llm_usage_events recipient_preferences preference_requests; do
  exists=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT to_regclass('public.${table}') IS NOT NULL")
  [ "$exists" = "t" ]
done

for col in own_occasion_id visibility reserved_by_user_id reserved_at reservation_expires_at purchased_by_user_id purchased_at share_status; do
  exists=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -Atc "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='wishlist_items' AND column_name='${col}')")
  [ "$exists" = "t" ]
done

echo "DRIFT_REPAIR_TEST_OK db=$DB_NAME final_head=$version"
