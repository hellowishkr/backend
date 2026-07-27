"""add vendor phone

Revision ID: b1d8f3a6c2e9
Revises: a9c4e7d1b2f6
Create Date: 2026-06-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1d8f3a6c2e9"
down_revision: Union[str, None] = "a9c4e7d1b2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("phone", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("vendors", "phone")
