"""add razorpay_short_url to orders

Revision ID: d8e3f1a9b274
Revises: c3a7b1d4f8e6
Create Date: 2026-04-28 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e3f1a9b274"
down_revision: Union[str, None] = "c3a7b1d4f8e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def upgrade() -> None:
    if not _column_exists("orders", "razorpay_short_url"):
        op.add_column("orders", sa.Column("razorpay_short_url", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "razorpay_short_url")
