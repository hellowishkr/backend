"""honeypot_hits: abuse-analytics ledger for /partners honeypot trips

Revision ID: d31e8a479c2b
Revises: c8d4f12a73e5
Create Date: 2026-04-29 13:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d31e8a479c2b"
down_revision: Union[str, None] = "c8d4f12a73e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table_name}"}).scalar())


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{index_name}"}).scalar())


def upgrade() -> None:
    if not _table_exists("honeypot_hits"):
        op.create_table(
            "honeypot_hits",
            sa.Column("id", sa.String(length=30), primary_key=True),
            sa.Column("endpoint", sa.String(length=60), nullable=False),
            sa.Column("ip", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("brand_name", sa.String(length=120), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not _index_exists("ix_honeypot_hits_endpoint"):
        op.create_index("ix_honeypot_hits_endpoint", "honeypot_hits", ["endpoint"])
    if not _index_exists("ix_honeypot_hits_created_at"):
        op.create_index("ix_honeypot_hits_created_at", "honeypot_hits", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_honeypot_hits_created_at", table_name="honeypot_hits")
    op.drop_index("ix_honeypot_hits_endpoint", table_name="honeypot_hits")
    op.drop_table("honeypot_hits")
