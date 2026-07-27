"""purchase provider-neutral fulfillment fields

Revision ID: 9d2f6a8c4b71
Revises: e4f6c81b9d72
Create Date: 2026-05-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9d2f6a8c4b71"
down_revision: Union[str, None] = "e4f6c81b9d72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{index_name}"}).scalar())


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass(:table_name) AND conname = :constraint_name
    """), {"table_name": f"public.{table_name}", "constraint_name": constraint_name}).scalar())


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE gifttype ADD VALUE IF NOT EXISTS 'EXPERIENCE'")
        op.execute("ALTER TYPE gifttype ADD VALUE IF NOT EXISTS 'PRIVATE_THEATRE'")

    _add_column_if_missing("purchases", sa.Column("provider", sa.String(length=40), nullable=True))
    _add_column_if_missing("purchases", sa.Column("provider_product_id", sa.String(length=80), nullable=True))
    _add_column_if_missing("purchases", sa.Column("provider_order_id", sa.String(length=80), nullable=True))
    _add_column_if_missing("purchases", sa.Column("fulfillment_policy", sa.String(length=40), nullable=True))
    _add_column_if_missing("purchases", sa.Column("fulfillment_lead_time_hours", sa.Integer(), nullable=True))
    _add_column_if_missing("purchases", sa.Column("fulfillment_due_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing(
        "purchases",
        sa.Column(
            "fulfillment_metadata_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )
    _add_column_if_missing("purchases", sa.Column("recipient_user_id", sa.String(length=30), nullable=True))
    _add_column_if_missing("purchases", sa.Column("recipient_phone", sa.String(length=15), nullable=True))
    _add_column_if_missing("purchases", sa.Column("delivery_address_id", sa.String(length=30), nullable=True))
    _add_column_if_missing("purchases", sa.Column("special_instructions", sa.Text(), nullable=True))

    if not _constraint_exists("purchases", "fk_purchases_recipient_user_id_users"):
        op.create_foreign_key(
            "fk_purchases_recipient_user_id_users",
            "purchases",
            "users",
            ["recipient_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _index_exists("ix_purchases_provider_due"):
        op.create_index("ix_purchases_provider_due", "purchases", ["provider", "fulfillment_due_at"])
    if not _index_exists("ix_purchases_recipient_user"):
        op.create_index("ix_purchases_recipient_user", "purchases", ["recipient_user_id"])

    op.execute(
        """
        UPDATE purchases
        SET
          provider = 'hubble',
          provider_product_id = COALESCE(provider_product_id, hubble_product_id),
          provider_order_id = COALESCE(provider_order_id, hubble_order_id),
          fulfillment_policy = COALESCE(
            fulfillment_policy,
            CASE
              WHEN delivery_scheduled_at IS NULL THEN 'IMMEDIATE_AFTER_PAYMENT'
              ELSE 'ON_DELIVERY_DATE'
            END
          ),
          fulfillment_due_at = COALESCE(
            fulfillment_due_at,
            CASE
              WHEN delivery_scheduled_at IS NULL THEN created_at
              ELSE delivery_scheduled_at
            END
          )
        WHERE gift_type = 'GIFT_CARD'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_purchases_recipient_user", table_name="purchases")
    op.drop_index("ix_purchases_provider_due", table_name="purchases")
    op.drop_constraint("fk_purchases_recipient_user_id_users", "purchases", type_="foreignkey")
    op.drop_column("purchases", "special_instructions")
    op.drop_column("purchases", "delivery_address_id")
    op.drop_column("purchases", "recipient_phone")
    op.drop_column("purchases", "recipient_user_id")
    op.drop_column("purchases", "fulfillment_metadata_json")
    op.drop_column("purchases", "fulfillment_due_at")
    op.drop_column("purchases", "fulfillment_lead_time_hours")
    op.drop_column("purchases", "fulfillment_policy")
    op.drop_column("purchases", "provider_order_id")
    op.drop_column("purchases", "provider_product_id")
    op.drop_column("purchases", "provider")
