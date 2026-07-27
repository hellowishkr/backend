"""product_pending_changes.created_product_id

Records the product created when a new-product vendor change is approved, so a
re-submit from the vendor's still-"new" editor folds into an edit instead of
creating a duplicate product.

Revision ID: d1e5a9c2b647
Revises: c9a4e1f7b3d2
Create Date: 2026-07-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e5a9c2b647"
down_revision: Union[str, None] = "c9a4e1f7b3d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_pending_changes",
        sa.Column("created_product_id", sa.String(length=30), nullable=True),
    )
    op.create_foreign_key(
        "fk_product_pending_changes_created_product_id",
        "product_pending_changes",
        "products",
        ["created_product_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_product_pending_changes_created_product_id",
        "product_pending_changes",
        ["created_product_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_pending_changes_created_product_id",
        table_name="product_pending_changes",
    )
    op.drop_constraint(
        "fk_product_pending_changes_created_product_id",
        "product_pending_changes",
        type_="foreignkey",
    )
    op.drop_column("product_pending_changes", "created_product_id")
