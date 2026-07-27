"""drop product delivery_available_pincodes

Revision ID: b7c4d8e2f1a9
Revises: a3e9b1c7d2f4
Create Date: 2026-06-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b7c4d8e2f1a9"
down_revision: str | Sequence[str] | None = "a3e9b1c7d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("products", "delivery_available_pincodes")


def downgrade() -> None:
    op.add_column(
        "products",
        sa.Column("delivery_available_pincodes", sa.Text(), nullable=True),
    )
