"""link purchase to wishlist item

Revision ID: c3a7b1d4f8e6
Revises: b2e5f9c8a1d3
Create Date: 2026-04-27 14:00:00.000000

Lets the Razorpay completion webhook stamp gifted_at on the wishlist
item that motivated the order, so suggest_gifts stops surfacing it
next year. Nullable because most orders aren't tied to a saved URL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3a7b1d4f8e6"
down_revision: Union[str, None] = "b2e5f9c8a1d3"
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


def upgrade() -> None:
    if not _column_exists("purchases", "wishlist_item_id"):
        op.add_column("purchases", sa.Column("wishlist_item_id", sa.String(length=30), nullable=True))
    if not _constraint_exists("purchases", "fk_purchases_wishlist_item"):
        op.create_foreign_key(
            "fk_purchases_wishlist_item",
            "purchases",
            "wishlist_items",
            ["wishlist_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _index_exists("ix_purchases_wishlist_item"):
        op.create_index(
            "ix_purchases_wishlist_item",
            "purchases",
            ["wishlist_item_id"],
            postgresql_where=sa.text("wishlist_item_id IS NOT NULL"),
        )


def downgrade() -> None:
    op.drop_index("ix_purchases_wishlist_item", table_name="purchases")
    op.drop_constraint("fk_purchases_wishlist_item", "purchases", type_="foreignkey")
    op.drop_column("purchases", "wishlist_item_id")
