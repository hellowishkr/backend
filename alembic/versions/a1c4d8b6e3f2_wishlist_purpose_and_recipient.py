"""wishlist purpose and recipient occasion

Revision ID: a1c4d8b6e3f2
Revises: f77cfe4f4b1e
Create Date: 2026-04-27 12:00:00.000000

Disambiguates self-vs-gift wishlist saves so a friend-flow paste
("save this for Sahil's birthday") doesn't land in the saver's
personal wishlist. Also lets the same URL coexist across purposes —
a user can want a product for themselves AND save it as a gift idea
for a friend without dedup collisions.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c4d8b6e3f2"
down_revision: Union[str, None] = "f77cfe4f4b1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wishlist_items",
        sa.Column(
            "purpose",
            sa.String(length=10),
            nullable=False,
            server_default="self",
        ),
    )
    # Polymorphic id — `occ_*` (user-tracked) or `cel_*` (celebrant occasion).
    # No FK because either parent table is valid; prefix conventions
    # disambiguate at read time.
    op.add_column(
        "wishlist_items",
        sa.Column("recipient_occasion_id", sa.String(length=30), nullable=True),
    )
    op.create_index(
        "ix_wishlist_recipient_occasion",
        "wishlist_items",
        ["recipient_occasion_id"],
        postgresql_where=sa.text("recipient_occasion_id IS NOT NULL"),
    )

    # Replace dedup indexes to be purpose-aware. Same URL can now exist
    # twice for one user if (purpose, recipient_occasion_id) differ.
    op.drop_index("uq_wishlist_user_url", table_name="wishlist_items")
    op.drop_index("uq_wishlist_user_product", table_name="wishlist_items")
    op.execute(
        "CREATE UNIQUE INDEX uq_wishlist_user_url "
        "ON wishlist_items (user_id, url, purpose, "
        "COALESCE(recipient_occasion_id, ''))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_wishlist_user_product "
        "ON wishlist_items (user_id, product_id, purpose, "
        "COALESCE(recipient_occasion_id, '')) "
        "WHERE product_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("uq_wishlist_user_product", table_name="wishlist_items")
    op.drop_index("uq_wishlist_user_url", table_name="wishlist_items")
    op.create_index(
        "uq_wishlist_user_product",
        "wishlist_items",
        ["user_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )
    op.create_index(
        "uq_wishlist_user_url",
        "wishlist_items",
        ["user_id", "url"],
        unique=True,
    )
    op.drop_index(
        "ix_wishlist_recipient_occasion", table_name="wishlist_items"
    )
    op.drop_column("wishlist_items", "recipient_occasion_id")
    op.drop_column("wishlist_items", "purpose")
