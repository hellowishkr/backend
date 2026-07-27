"""gift_log table

Revision ID: d4e8f1a92c63
Revises: c2e8a4f6b9d3
Create Date: 2026-05-29 10:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e8f1a92c63"
down_revision: Union[str, None] = "c2e8a4f6b9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = :table_name
    """), {"table_name": table_name}).scalar())


def upgrade() -> None:
    if not _table_exists("gift_log"):
        op.create_table(
            "gift_log",
            sa.Column("id", sa.String(length=30), primary_key=True),
            sa.Column("user_id", sa.String(length=30), nullable=False),
            sa.Column("contact_id", sa.String(length=30), nullable=True),
            sa.Column("occasion_id", sa.String(length=30), nullable=True),
            sa.Column("description", sa.String(length=200), nullable=False),
            sa.Column("gift_year", sa.String(length=4), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["occasion_id"], ["occasions.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_gift_log_user_id", "gift_log", ["user_id"])
        op.create_index("ix_gift_log_contact_id", "gift_log", ["contact_id"])
        op.create_index("ix_gift_log_user_contact", "gift_log", ["user_id", "contact_id"])


def downgrade() -> None:
    if _table_exists("gift_log"):
        op.drop_index("ix_gift_log_user_contact", table_name="gift_log")
        op.drop_index("ix_gift_log_contact_id", table_name="gift_log")
        op.drop_index("ix_gift_log_user_id", table_name="gift_log")
        op.drop_table("gift_log")
