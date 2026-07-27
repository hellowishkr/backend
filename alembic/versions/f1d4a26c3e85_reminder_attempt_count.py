"""reminders: attempt_count + last_error for transient retry

Revision ID: f1d4a26c3e85
Revises: e3c1f9a8d72b
Create Date: 2026-04-28 16:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1d4a26c3e85"
down_revision: Union[str, None] = "e3c1f9a8d72b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def upgrade() -> None:
    if not _column_exists("reminders", "attempt_count"):
        op.add_column("reminders", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("reminders", "attempt_count", server_default=None)
    if not _column_exists("reminders", "last_error"):
        op.add_column("reminders", sa.Column("last_error", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("reminders", "last_error")
    op.drop_column("reminders", "attempt_count")
