"""order marketing attribution

Revision ID: c6d1a9e4b2f7
Revises: a4f8c2d1e6b9
Create Date: 2026-07-06 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6d1a9e4b2f7"
down_revision: Union[str, None] = "a4f8c2d1e6b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("marketing_attribution", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "marketing_attribution")
