"""add vendor flat shipping fee

Revision ID: a6e4c2f8b901
Revises: f2a9c3e58d10
Create Date: 2026-07-21

When configured, shipping_flat_fee_paise is the complete shipping charge for
one single-vendor order and does not increase with product quantity.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a6e4c2f8b901"
down_revision: Union[str, None] = "f2a9c3e58d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vendors",
        sa.Column("shipping_flat_fee_paise", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vendors", "shipping_flat_fee_paise")
