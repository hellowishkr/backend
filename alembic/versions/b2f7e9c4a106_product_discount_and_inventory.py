"""add product and variant discounted prices and inventory quantities

Revision ID: b2f7e9c4a106
Revises: a6e4c2f8b901
Create Date: 2026-07-25

Null inventory means unlimited/untracked stock. Existing rows are left null so
the migration preserves their current availability until a catalogue source
provides an exact quantity.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2f7e9c4a106"
down_revision: Union[str, None] = "a6e4c2f8b901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("discounted_price_paise", sa.Integer(), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("inventory_quantity", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_products_discount_not_above_base",
        "products",
        "discounted_price_paise IS NULL OR discounted_price_paise <= base_price_paise",
    )
    op.create_check_constraint(
        "ck_products_inventory_nonnegative",
        "products",
        "inventory_quantity IS NULL OR inventory_quantity >= 0",
    )

    op.add_column(
        "product_variants",
        sa.Column("discounted_price_paise", sa.Integer(), nullable=True),
    )
    op.add_column(
        "product_variants",
        sa.Column("inventory_quantity", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_product_variants_discount_not_above_base",
        "product_variants",
        "discounted_price_paise IS NULL OR discounted_price_paise <= price_delta_paise",
    )
    op.create_check_constraint(
        "ck_product_variants_inventory_nonnegative",
        "product_variants",
        "inventory_quantity IS NULL OR inventory_quantity >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_product_variants_inventory_nonnegative",
        "product_variants",
        type_="check",
    )
    op.drop_constraint(
        "ck_product_variants_discount_not_above_base",
        "product_variants",
        type_="check",
    )
    op.drop_column("product_variants", "inventory_quantity")
    op.drop_column("product_variants", "discounted_price_paise")

    op.drop_constraint(
        "ck_products_inventory_nonnegative",
        "products",
        type_="check",
    )
    op.drop_constraint(
        "ck_products_discount_not_above_base",
        "products",
        type_="check",
    )
    op.drop_column("products", "inventory_quantity")
    op.drop_column("products", "discounted_price_paise")
