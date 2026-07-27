"""add admin_users, notification_templates, audit_logs

Revision ID: c3e9b27f1a85
Revises: b2d6f1a40c73
Create Date: 2026-06-21 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3e9b27f1a85"
down_revision: Union[str, None] = "b2d6f1a40c73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False, server_default="ops"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"], unique=True)

    op.create_table(
        "notification_templates",
        sa.Column("key", sa.String(length=60), primary_key=True),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=True),
        sa.Column("entity_id", sa.String(length=40), nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("notification_templates")
    op.drop_index("ix_admin_users_email", table_name="admin_users")
    op.drop_table("admin_users")
