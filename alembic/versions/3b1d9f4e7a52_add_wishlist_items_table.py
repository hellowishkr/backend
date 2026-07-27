"""add wishlist_items table

Revision ID: 3b1d9f4e7a52
Revises: cf0285589097
Create Date: 2026-04-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3b1d9f4e7a52"
down_revision: Union[str, None] = "cf0285589097"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "wishlist_items",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("user_id", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.String(length=60), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index(
        op.f("ix_wishlist_items_user_id"),
        "wishlist_items",
        ["user_id"],
    )
    # Strong dedup: same normalized URL twice per user is a violation.
    op.create_index(
        "uq_wishlist_user_url",
        "wishlist_items",
        ["user_id", "url"],
        unique=True,
    )
    # Stronger dedup when we have a provider-native id. Partial index so rows
    # with NULL product_id don't collide with each other.
    op.create_index(
        "uq_wishlist_user_product",
        "wishlist_items",
        ["user_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("product_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_wishlist_user_product", table_name="wishlist_items")
    op.drop_index("uq_wishlist_user_url", table_name="wishlist_items")
    op.drop_index(op.f("ix_wishlist_items_user_id"), table_name="wishlist_items")
    op.drop_table("wishlist_items")
