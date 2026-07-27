"""addon quantity config + order line item add-on snapshot

Adds allow_quantity/min_quantity/max_quantity to product_addons and replaces
order_line_items.addon_ids (bare id list) with selected_addons (a frozen
snapshot of name/unit_price/qty/total/note per chosen add-on).

There are no live vendors or running orders, so this is a clean breaking
change: addon_ids is dropped outright rather than migrated.

Revision ID: c9a1f7e2d3b8
Revises: a8f3d21b9c47
Create Date: 2026-06-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9a1f7e2d3b8"
down_revision: Union[str, None] = "a8f3d21b9c47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_addons",
        sa.Column("allow_quantity", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "product_addons",
        sa.Column("min_quantity", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "product_addons",
        sa.Column("max_quantity", sa.Integer(), nullable=False, server_default="1"),
    )

    op.add_column(
        "order_line_items",
        sa.Column("selected_addons", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.drop_column("order_line_items", "addon_ids")


def downgrade() -> None:
    op.add_column(
        "order_line_items",
        sa.Column("addon_ids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.drop_column("order_line_items", "selected_addons")

    op.drop_column("product_addons", "max_quantity")
    op.drop_column("product_addons", "min_quantity")
    op.drop_column("product_addons", "allow_quantity")
