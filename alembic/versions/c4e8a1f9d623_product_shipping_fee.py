"""add shipping_included/shipping_fee_paise to products, shipping_fee_paise to order_line_items

Revision ID: c4e8a1f9d623
Revises: b7d2e94a3f61
Create Date: 2026-06-24 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4e8a1f9d623"
down_revision: Union[str, None] = "b7d2e94a3f61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("shipping_included", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column("products", sa.Column("shipping_fee_paise", sa.Integer(), nullable=True))
    op.add_column(
        "order_line_items",
        sa.Column("shipping_fee_paise", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("order_line_items", "shipping_fee_paise")
    op.drop_column("products", "shipping_fee_paise")
    op.drop_column("products", "shipping_included")
