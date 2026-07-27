"""users: last_inbound_at for 24h CS-window detection

Revision ID: b7a3e5d219c4
Revises: f1d4a26c3e85
Create Date: 2026-04-29 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7a3e5d219c4"
down_revision: Union[str, None] = "f1d4a26c3e85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def upgrade() -> None:
    if not _column_exists("users", "last_inbound_at"):
        op.add_column("users", sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE users
        SET last_inbound_at = updated_at
        WHERE last_inbound_at IS NULL
          AND updated_at > now() - interval '24 hours'
        """
    )


def downgrade() -> None:
    op.drop_column("users", "last_inbound_at")
