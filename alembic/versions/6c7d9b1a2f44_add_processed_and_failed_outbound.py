"""add processed message idempotency and failed outbound audit

Revision ID: 6c7d9b1a2f44
Revises: 3b1d9f4e7a52
Create Date: 2026-04-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6c7d9b1a2f44"
down_revision: Union[str, None] = "3b1d9f4e7a52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("wamid", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("wamid"),
    )

    op.create_table(
        "failed_outbound_messages",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("phone", sa.String(length=15), nullable=False),
        sa.Column("message_body", sa.Text(), nullable=False),
        sa.Column("send_kind", sa.String(length=30), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_failed_outbound_messages_phone"),
        "failed_outbound_messages",
        ["phone"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_failed_outbound_messages_phone"),
        table_name="failed_outbound_messages",
    )
    op.drop_table("failed_outbound_messages")
    op.drop_table("processed_messages")
