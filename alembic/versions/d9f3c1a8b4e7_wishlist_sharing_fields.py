"""Add wishlist sharing fields: own_occasion_id, visibility, reservation, purchase_status.

Revision ID: d9f3c1a8b4e7
Revises: b2e5f9c8a1d3
Create Date: 2026-05-08

Changes:
  - own_occasion_id (nullable str) — links a self-wishlist item to the
    owner's own occasion (birthday, wedding, etc.) for occasion-scoped
    personal wishlists.
  - visibility (str, default 'private') — controls who can see a
    purpose='self' item: 'private', 'friends', or 'public'.
  - reserved_by_user_id, reserved_at, reservation_expires_at — friend
    reservation/claim before purchase to prevent double-buying.
  - purchased_by_user_id, purchased_at — final purchase confirmation.
  - share_status (str, default 'available') — machine-readable lifecycle:
    available | reserved | purchased | expired.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d9f3c1a8b4e7"
down_revision = "b2e5f9c8a1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All new columns are nullable or have safe defaults — safe for
    # existing data (no downtime on a live table).

    op.add_column(
        "wishlist_items",
        sa.Column("own_occasion_id", sa.String(30), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("visibility", sa.String(10), nullable=False, server_default="private"),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("reserved_by_user_id", sa.String(30), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("purchased_by_user_id", sa.String(30), nullable=True),
    )
    op.add_column(
        "wishlist_items",
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Add share_status as nullable first so the backfill can run, then
    # set NOT NULL with the default for new rows.
    op.add_column(
        "wishlist_items",
        sa.Column("share_status", sa.String(12), nullable=True),
    )

    # Backfill: derive share_status from existing gifted_at.
    # Rows with gifted_at set were fulfilled → 'purchased'.
    # All others → 'available'.
    op.execute(
        sa.text(
            "UPDATE wishlist_items SET share_status = "
            "CASE WHEN gifted_at IS NOT NULL THEN 'purchased' ELSE 'available' END"
        )
    )

    # Now make the column NOT NULL with the server default for future rows.
    op.alter_column(
        "wishlist_items",
        "share_status",
        nullable=False,
        server_default="available",
    )

    op.create_index(
        "ix_wishlist_own_occasion",
        "wishlist_items",
        ["own_occasion_id"],
        postgresql_where=sa.text("own_occasion_id IS NOT NULL"),
    )
    op.create_index(
        "ix_wishlist_reserved",
        "wishlist_items",
        ["reserved_at"],
        postgresql_where=sa.text(
            "reserved_by_user_id IS NOT NULL AND purchased_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_wishlist_reserved", table_name="wishlist_items")
    op.drop_index("ix_wishlist_own_occasion", table_name="wishlist_items")
    op.drop_column("wishlist_items", "share_status")
    op.drop_column("wishlist_items", "purchased_at")
    op.drop_column("wishlist_items", "purchased_by_user_id")
    op.drop_column("wishlist_items", "reservation_expires_at")
    op.drop_column("wishlist_items", "reserved_at")
    op.drop_column("wishlist_items", "reserved_by_user_id")
    op.drop_column("wishlist_items", "visibility")
    op.drop_column("wishlist_items", "own_occasion_id")
