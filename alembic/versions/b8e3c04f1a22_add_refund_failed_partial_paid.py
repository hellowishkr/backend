"""add REFUND_FAILED and PARTIALLY_PAID to orderstatus

Revision ID: b8e3c04f1a22
Revises: a7f2d91b4c80
Create Date: 2026-04-18 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b8e3c04f1a22'
down_revision: Union[str, None] = 'a7f2d91b4c80'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'REFUND_FAILED'")
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_PAID'")


def downgrade() -> None:
    # Postgres cannot cleanly remove enum values — leave them (benign)
    pass
