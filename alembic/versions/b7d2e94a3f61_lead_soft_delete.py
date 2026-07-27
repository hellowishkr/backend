"""add deleted_at soft-delete column to vendor_leads and corporate_leads

Revision ID: b7d2e94a3f61
Revises: a4f8c2d61b93
Create Date: 2026-06-24 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d2e94a3f61"
down_revision: Union[str, None] = "a4f8c2d61b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendor_leads", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("corporate_leads", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("corporate_leads", "deleted_at")
    op.drop_column("vendor_leads", "deleted_at")
