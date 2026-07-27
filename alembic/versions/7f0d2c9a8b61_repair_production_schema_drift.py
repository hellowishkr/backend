"""repair production schema drift before LLM usage events

Revision ID: 7f0d2c9a8b61
Revises: a1b3c2d4e5f6
Create Date: 2026-05-10 19:00:00.000000

This migration repairs the current production database, whose alembic_version
was advanced to a1b3c2d4e5f6 while some earlier wishlist/preference objects
were absent. It is intentionally idempotent so clean databases no-op here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7f0d2c9a8b61"
down_revision: Union[str, None] = "a1b3c2d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table_name}"}).scalar())


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name
          AND column_name = :column_name
        """
    ), {"table_name": table_name, "column_name": column_name}).scalar())


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{index_name}"}).scalar())


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def _ensure_wishlist_sharing_columns() -> None:
    _add_column_if_missing("wishlist_items", sa.Column("own_occasion_id", sa.String(30), nullable=True))
    _add_column_if_missing("wishlist_items", sa.Column("visibility", sa.String(10), nullable=False, server_default="private"))
    _add_column_if_missing("wishlist_items", sa.Column("reserved_by_user_id", sa.String(30), nullable=True))
    _add_column_if_missing("wishlist_items", sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing("wishlist_items", sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing("wishlist_items", sa.Column("purchased_by_user_id", sa.String(30), nullable=True))
    _add_column_if_missing("wishlist_items", sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True))
    if not _column_exists("wishlist_items", "share_status"):
        op.add_column("wishlist_items", sa.Column("share_status", sa.String(12), nullable=True))
        op.execute(
            sa.text(
                "UPDATE wishlist_items SET share_status = "
                "CASE WHEN gifted_at IS NOT NULL THEN 'purchased' ELSE 'available' END"
            )
        )
        op.alter_column("wishlist_items", "share_status", nullable=False, server_default="available")

    if not _index_exists("ix_wishlist_own_occasion"):
        op.create_index(
            "ix_wishlist_own_occasion",
            "wishlist_items",
            ["own_occasion_id"],
            postgresql_where=sa.text("own_occasion_id IS NOT NULL"),
        )
    if not _index_exists("ix_wishlist_reserved"):
        op.create_index(
            "ix_wishlist_reserved",
            "wishlist_items",
            ["reserved_at"],
            postgresql_where=sa.text("reserved_by_user_id IS NOT NULL AND purchased_at IS NULL"),
        )


def _ensure_preference_tables() -> None:
    if not _table_exists("recipient_preferences"):
        op.create_table(
            "recipient_preferences",
            sa.Column("id", sa.String(30), primary_key=True),
            sa.Column("recipient_phone", sa.String(15), nullable=False, unique=True),
            sa.Column("preferred_categories", sa.Text(), nullable=True),
            sa.Column("disliked_categories", sa.Text(), nullable=True),
            sa.Column("preferred_brands", sa.Text(), nullable=True),
            sa.Column("disliked_brands", sa.Text(), nullable=True),
            sa.Column("preferred_budget_min", sa.Integer(), nullable=True),
            sa.Column("preferred_budget_max", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("wishlist_links", sa.Text(), nullable=True),
            sa.Column("source", sa.String(20), nullable=False, server_default="direct_response"),
            sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        )
    if not _index_exists("ix_recip_pref_phone"):
        op.create_index("ix_recip_pref_phone", "recipient_preferences", ["recipient_phone"])

    if not _table_exists("preference_requests"):
        op.create_table(
            "preference_requests",
            sa.Column("id", sa.String(30), primary_key=True),
            sa.Column("recipient_phone", sa.String(15), nullable=False),
            sa.Column("occasion_id", sa.String(30), nullable=True),
            sa.Column("requester_phone", sa.String(15), nullable=False),
            sa.Column("notify_phones", sa.Text(), nullable=True),
            sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
            sa.Column("anonymous", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _index_exists("ix_pref_req_recipient"):
        op.create_index("ix_pref_req_recipient", "preference_requests", ["recipient_phone"])
    if not _index_exists("ix_pref_req_requester"):
        op.create_index("ix_pref_req_requester", "preference_requests", ["requester_phone"])
    if not _index_exists("ix_pref_req_pending"):
        op.create_index(
            "ix_pref_req_pending",
            "preference_requests",
            ["recipient_phone", "occasion_id"],
            postgresql_where=sa.text("status = 'pending'"),
        )


def _ensure_platform_fee_columns() -> None:
    _add_column_if_missing("orders", sa.Column("gift_amount", sa.Numeric(10, 2), nullable=True))
    _add_column_if_missing("orders", sa.Column("platform_fee", sa.Numeric(10, 2), nullable=True))
    _add_column_if_missing("orders", sa.Column("platform_fee_gst", sa.Numeric(10, 2), nullable=True))


def upgrade() -> None:
    _ensure_wishlist_sharing_columns()
    _ensure_preference_tables()
    _ensure_platform_fee_columns()


def downgrade() -> None:
    # Intentional no-op: this repair exists to reconcile production metadata
    # with already-present schema. Dropping repaired objects could destroy data.
    pass
