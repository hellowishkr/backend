"""gifting structured product fields

Adds structured occasions + delivery pincodes, dispatch lead time, and
bestseller/customisable flags to products. Powers the Epic 8 occasion filter,
PDP pincode check, delivery-speed filter, and card badges.

Revision ID: a3e9b1c7d2f4
Revises: 4f7c2a9d6e10
Create Date: 2026-06-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3e9b1c7d2f4"
down_revision: Union[str, None] = "4f7c2a9d6e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("occasions", sa.JSON(), nullable=True))
    op.add_column("products", sa.Column("dispatch_lead_time_days", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("delivery_pincodes", sa.JSON(), nullable=True))
    op.add_column(
        "products",
        sa.Column("is_bestseller", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "products",
        sa.Column("is_customisable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(op.f("ix_products_is_bestseller"), "products", ["is_bestseller"], unique=False)
    # Backfill is_customisable from existing customization schema.
    op.execute(
        "UPDATE products SET is_customisable = true "
        "WHERE customization_schema IS NOT NULL "
        "AND customization_schema::text NOT IN ('[]', 'null')"
    )
    # Drop the server_default now that existing rows are populated; app sets it explicitly.
    op.alter_column("products", "is_bestseller", server_default=None)
    op.alter_column("products", "is_customisable", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_products_is_bestseller"), table_name="products")
    op.drop_column("products", "is_customisable")
    op.drop_column("products", "is_bestseller")
    op.drop_column("products", "delivery_pincodes")
    op.drop_column("products", "dispatch_lead_time_days")
    op.drop_column("products", "occasions")
