"""feedback table + vendor_leads.country

Revision ID: c2e8a4f6b9d3
Revises: 817a44a3b3bb
Create Date: 2026-05-23 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2e8a4f6b9d3"
down_revision: Union[str, None] = "817a44a3b3bb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = :table_name
    """), {"table_name": table_name}).scalar())


def upgrade() -> None:
    if not _column_exists("vendor_leads", "country"):
        op.add_column("vendor_leads", sa.Column("country", sa.String(length=8), nullable=True))
        op.create_index("ix_vendor_leads_country", "vendor_leads", ["country"])

    if not _table_exists("feedback"):
        op.create_table(
            "feedback",
            sa.Column("id", sa.String(length=30), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("category", sa.String(length=60), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=True),
            sa.Column("app_version", sa.String(length=40), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("ip", sa.String(length=45), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="new"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_feedback_email", "feedback", ["email"])
        op.create_index("ix_feedback_category", "feedback", ["category"])
        op.create_index("ix_feedback_source", "feedback", ["source"])
        op.create_index("ix_feedback_status", "feedback", ["status"])


def downgrade() -> None:
    if _table_exists("feedback"):
        op.drop_index("ix_feedback_status", table_name="feedback")
        op.drop_index("ix_feedback_source", table_name="feedback")
        op.drop_index("ix_feedback_category", table_name="feedback")
        op.drop_index("ix_feedback_email", table_name="feedback")
        op.drop_table("feedback")
    if _column_exists("vendor_leads", "country"):
        op.drop_index("ix_vendor_leads_country", table_name="vendor_leads")
        op.drop_column("vendor_leads", "country")
