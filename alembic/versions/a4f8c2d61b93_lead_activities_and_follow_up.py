"""add lead_activities timeline table and next_follow_up_at on leads

Revision ID: a4f8c2d61b93
Revises: c3e9b27f1a85
Create Date: 2026-06-23 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f8c2d61b93"
down_revision: Union[str, None] = "c3e9b27f1a85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lead_activities",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("lead_kind", sa.String(length=20), nullable=False),
        sa.Column("lead_id", sa.String(length=30), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_lead_activities_lead_kind", "lead_activities", ["lead_kind"])
    op.create_index("ix_lead_activities_lead_id", "lead_activities", ["lead_id"])

    op.add_column("vendor_leads", sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("corporate_leads", sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("corporate_leads", "next_follow_up_at")
    op.drop_column("vendor_leads", "next_follow_up_at")
    op.drop_index("ix_lead_activities_lead_id", table_name="lead_activities")
    op.drop_index("ix_lead_activities_lead_kind", table_name="lead_activities")
    op.drop_table("lead_activities")
