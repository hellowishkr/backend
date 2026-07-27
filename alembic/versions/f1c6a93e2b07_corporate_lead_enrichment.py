"""add logo_url, delivery_type, needed_by to corporate_leads

Revision ID: f1c6a93e2b07
Revises: e91a4c7f2d68
Create Date: 2026-06-24 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1c6a93e2b07"
down_revision: Union[str, None] = "e91a4c7f2d68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("corporate_leads", sa.Column("logo_url", sa.String(length=500), nullable=True))
    op.add_column("corporate_leads", sa.Column("delivery_type", sa.String(length=20), nullable=True))
    op.add_column("corporate_leads", sa.Column("needed_by", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("corporate_leads", "needed_by")
    op.drop_column("corporate_leads", "delivery_type")
    op.drop_column("corporate_leads", "logo_url")
