"""add video_url to products

Revision ID: d7f2b3a8e145
Revises: c4e8a1f9d623
Create Date: 2026-06-24 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f2b3a8e145"
down_revision: Union[str, None] = "c4e8a1f9d623"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("video_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "video_url")
