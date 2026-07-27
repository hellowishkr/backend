"""gifting ux product metadata

Revision ID: 4f7c2a9d6e10
Revises: e2b19967cf98
Create Date: 2026-06-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4f7c2a9d6e10"
down_revision: Union[str, None] = "e2b19967cf98"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("category", sa.String(length=60), nullable=True))
    op.create_index(op.f("ix_products_category"), "products", ["category"], unique=False)
    op.add_column("products", sa.Column("preparation_time", sa.String(length=120), nullable=True))
    op.add_column("products", sa.Column("delivery_available_pincodes", sa.Text(), nullable=True))
    op.add_column("products", sa.Column("product_images", sa.JSON(), nullable=True))
    op.add_column("products", sa.Column("care_instructions", sa.Text(), nullable=True))
    op.add_column("products", sa.Column("whats_included", sa.Text(), nullable=True))
    op.add_column("products", sa.Column("customization_schema", sa.JSON(), nullable=True))
    op.add_column("orders", sa.Column("gift_message", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("is_gift", sa.Boolean(), nullable=True))
    op.add_column("orders", sa.Column("hide_prices", sa.Boolean(), nullable=True))
    op.execute("UPDATE orders SET is_gift = true WHERE is_gift IS NULL")
    op.execute("UPDATE orders SET hide_prices = true WHERE hide_prices IS NULL")
    op.alter_column("orders", "is_gift", nullable=False)
    op.alter_column("orders", "hide_prices", nullable=False)
    op.add_column("order_line_items", sa.Column("customizations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("order_line_items", "customizations")
    op.drop_column("orders", "hide_prices")
    op.drop_column("orders", "is_gift")
    op.drop_column("orders", "gift_message")
    op.drop_column("products", "customization_schema")
    op.drop_column("products", "whats_included")
    op.drop_column("products", "care_instructions")
    op.drop_column("products", "product_images")
    op.drop_column("products", "delivery_available_pincodes")
    op.drop_column("products", "preparation_time")
    op.drop_index(op.f("ix_products_category"), table_name="products")
    op.drop_column("products", "category")
