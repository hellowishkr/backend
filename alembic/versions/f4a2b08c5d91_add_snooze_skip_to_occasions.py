"""add snooze_until and skipped_year to occasions

Revision ID: f4a2b08c5d91
Revises: e9f1c27a8b04
Create Date: 2026-04-20 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a2b08c5d91"
down_revision: Union[str, None] = "e9f1c27a8b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "occasions",
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "occasions",
        sa.Column("skipped_year", sa.String(length=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("occasions", "skipped_year")
    op.drop_column("occasions", "snooze_until")
