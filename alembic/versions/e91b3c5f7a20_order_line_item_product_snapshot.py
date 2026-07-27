"""order_line_items product name/image snapshot

Confirmation pages and order history read product_name/image_url from the
live Product row via order_line_items.product_id, so editing a product
(rename, new photo) afterwards silently rewrites what a past order shows —
the same class of bug selected_addons was already snapshotted to avoid.
Adds product_name/product_image_url captured at checkout time; existing rows
are backfilled from the current Product row (best-effort — it's the closest
available truth for pre-existing orders, though the product may already have
changed since the order was placed).

Revision ID: e91b3c5f7a20
Revises: d84a6f1e2b73
Create Date: 2026-07-05 00:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e91b3c5f7a20"
down_revision: Union[str, None] = "d84a6f1e2b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_line_items",
        sa.Column("product_name", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "order_line_items",
        sa.Column("product_image_url", sa.String(length=500), nullable=True),
    )
    op.execute(
        """
        UPDATE order_line_items AS oli
        SET product_name = p.name,
            product_image_url = p.image_url
        FROM products AS p
        WHERE oli.product_id = p.id
        """
    )
    # Any line item whose product was deleted before this migration ran has no
    # source of truth left to backfill from — fall back to a placeholder
    # rather than leaving NOT NULL unsatisfiable.
    op.execute(
        "UPDATE order_line_items SET product_name = 'Product' WHERE product_name IS NULL"
    )
    op.alter_column("order_line_items", "product_name", nullable=False)


def downgrade() -> None:
    op.drop_column("order_line_items", "product_image_url")
    op.drop_column("order_line_items", "product_name")
