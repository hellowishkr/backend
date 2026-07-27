"""wishlist lifecycle + claim linkage

Revision ID: b2e5f9c8a1d3
Revises: a1c4d8b6e3f2
Create Date: 2026-04-27 13:00:00.000000

Adds the missing lifecycle fields so wishlist items don't get
re-suggested year over year, dead links surface as broken instead of
silently failing, and date-scheduled reminders ("remind me on April 29")
are persisted. Also gives external_gift_claims an FK back to the
wishlist item that was fulfilled, closing the dedupe loop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2e5f9c8a1d3"
down_revision: Union[str, None] = "a1c4d8b6e3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Wishlist lifecycle columns
    op.add_column(
        "wishlist_items",
        sa.Column("gifted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("gifted_order_id", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("unavailable_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("remind_on", sa.Date(), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Indexes that worker queries will hit. Partial so unset rows don't bloat.
    op.create_index(
        "ix_wishlist_remind_pending",
        "wishlist_items",
        ["remind_on"],
        postgresql_where=sa.text("remind_on IS NOT NULL AND reminded_at IS NULL"),
    )
    op.create_index(
        "ix_wishlist_validation_due",
        "wishlist_items",
        ["last_validated_at"],
        postgresql_where=sa.text("unavailable_at IS NULL"),
    )

    # Close the dedupe loop: claims can now name the wishlist item they
    # fulfilled. Nullable because legacy claims (free-text-only) stay valid.
    op.add_column(
        "external_gift_claims",
        sa.Column("wishlist_item_id", sa.String(length=30), nullable=True),
    )
    op.create_foreign_key(
        "fk_claims_wishlist_item",
        "external_gift_claims",
        "wishlist_items",
        ["wishlist_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_claims_wishlist_item",
        "external_gift_claims",
        ["wishlist_item_id"],
        postgresql_where=sa.text("wishlist_item_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_claims_wishlist_item", table_name="external_gift_claims")
    op.drop_constraint(
        "fk_claims_wishlist_item", "external_gift_claims", type_="foreignkey",
    )
    op.drop_column("external_gift_claims", "wishlist_item_id")

    op.drop_index("ix_wishlist_validation_due", table_name="wishlist_items")
    op.drop_index("ix_wishlist_remind_pending", table_name="wishlist_items")
    op.drop_column("wishlist_items", "reminded_at")
    op.drop_column("wishlist_items", "remind_on")
    op.drop_column("wishlist_items", "unavailable_at")
    op.drop_column("wishlist_items", "last_validated_at")
    op.drop_column("wishlist_items", "gifted_order_id")
    op.drop_column("wishlist_items", "gifted_at")
