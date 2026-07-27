"""failed_outbound_messages: template_name + template_params_json

Lets the retry job replay voucher sends as templates when the original send
was templated. Without these columns, voucher confirmations that fail outside
the 24h CS window cannot be replayed (Meta rejects free-text).

Revision ID: c8d4f12a73e5
Revises: b7a3e5d219c4
Create Date: 2026-04-29 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d4f12a73e5"
down_revision: Union[str, None] = "b7a3e5d219c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return bool(bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def upgrade() -> None:
    if not _column_exists("failed_outbound_messages", "template_name"):
        op.add_column("failed_outbound_messages", sa.Column("template_name", sa.String(length=100), nullable=True))
    if not _column_exists("failed_outbound_messages", "template_params_json"):
        op.add_column("failed_outbound_messages", sa.Column("template_params_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("failed_outbound_messages", "template_params_json")
    op.drop_column("failed_outbound_messages", "template_name")
