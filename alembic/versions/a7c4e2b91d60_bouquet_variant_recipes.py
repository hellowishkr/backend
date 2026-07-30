"""add bouquet recipes and seasonal variant pricing

Revision ID: a7c4e2b91d60
Revises: d6b2f8a4c901
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e2b91d60"
down_revision: Union[str, None] = "d6b2f8a4c901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_variants",
        sa.Column("seasonal_price_paise", sa.Integer(), nullable=True),
    )
    op.add_column(
        "product_variants",
        sa.Column("seasonal_price_start", sa.Date(), nullable=True),
    )
    op.add_column(
        "product_variants",
        sa.Column("seasonal_price_end", sa.Date(), nullable=True),
    )
    op.add_column(
        "product_variants",
        sa.Column("bouquet_recipe", sa.JSON(), nullable=True),
    )
    op.create_check_constraint(
        "ck_product_variants_seasonal_price_nonnegative",
        "product_variants",
        "seasonal_price_paise IS NULL OR seasonal_price_paise >= 0",
    )
    op.create_check_constraint(
        "ck_product_variants_seasonal_window",
        "product_variants",
        "seasonal_price_start IS NULL OR seasonal_price_end IS NULL "
        "OR seasonal_price_start <= seasonal_price_end",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_product_variants_seasonal_window",
        "product_variants",
        type_="check",
    )
    op.drop_constraint(
        "ck_product_variants_seasonal_price_nonnegative",
        "product_variants",
        type_="check",
    )
    op.drop_column("product_variants", "bouquet_recipe")
    op.drop_column("product_variants", "seasonal_price_end")
    op.drop_column("product_variants", "seasonal_price_start")
    op.drop_column("product_variants", "seasonal_price_paise")
