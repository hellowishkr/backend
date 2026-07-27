"""Add platform_fee columns to orders

Revision ID: a1b3c2d4e5f6
Revises: e1a4b7c9d2f5
Create Date: 2026-05-04 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b3c2d4e5f6"
down_revision: Union[str, None] = "e1a4b7c9d2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("gift_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("orders", sa.Column("platform_fee", sa.Numeric(10, 2), nullable=True))
    op.add_column("orders", sa.Column("platform_fee_gst", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "platform_fee_gst")
    op.drop_column("orders", "platform_fee")
    op.drop_column("orders", "gift_amount")
