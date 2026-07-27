"""add status tracking to processed messages

Revision ID: 1f2a6d8b9c33
Revises: 6c7d9b1a2f44
Create Date: 2026-04-23 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1f2a6d8b9c33"
down_revision: Union[str, None] = "6c7d9b1a2f44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "processed_messages",
        sa.Column("status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "processed_messages",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE processed_messages SET status = 'done' WHERE status IS NULL")
    op.execute("UPDATE processed_messages SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column(
        "processed_messages",
        "status",
        nullable=False,
        server_default="processing",
    )
    op.alter_column(
        "processed_messages",
        "updated_at",
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.drop_column("processed_messages", "updated_at")
    op.drop_column("processed_messages", "status")
