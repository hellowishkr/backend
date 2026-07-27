"""product.in_stock — vendor-controlled availability flag

Revision ID: c9a4e1f7b3d2
Revises: b7e2d4f1a9c3
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9a4e1f7b3d2"
down_revision: Union[str, None] = "b7e2d4f1a9c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "in_stock",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "in_stock")
