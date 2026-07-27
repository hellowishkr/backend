"""add campaign_contexts

Revision ID: 1c9b42ef8d5a
Revises: c6d1a9e4b2f7
Create Date: 2026-07-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1c9b42ef8d5a"
down_revision: Union[str, None] = "c6d1a9e4b2f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_contexts",
        sa.Column("phone", sa.String(length=15), nullable=False),
        sa.Column("template_name", sa.String(length=100), nullable=False),
        sa.Column("campaign_source", sa.String(length=100), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("phone"),
    )


def downgrade() -> None:
    op.drop_table("campaign_contexts")
