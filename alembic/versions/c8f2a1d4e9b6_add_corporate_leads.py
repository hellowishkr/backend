"""add corporate leads

Revision ID: c8f2a1d4e9b6
Revises: b7c4d8e2f1a9
Create Date: 2026-06-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "c8f2a1d4e9b6"
down_revision: str | Sequence[str] | None = "b7c4d8e2f1a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_leads",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("company_name", sa.String(length=160), nullable=False),
        sa.Column("contact_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=True),
        sa.Column("recipient_count", sa.Integer(), nullable=True),
        sa.Column("occasion", sa.String(length=80), nullable=True),
        sa.Column("budget_per_recipient", sa.String(length=80), nullable=True),
        sa.Column("timeline", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("consent_given", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_corporate_leads_email"), "corporate_leads", ["email"])
    op.create_index(op.f("ix_corporate_leads_status"), "corporate_leads", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_corporate_leads_status"), table_name="corporate_leads")
    op.drop_index(op.f("ix_corporate_leads_email"), table_name="corporate_leads")
    op.drop_table("corporate_leads")
