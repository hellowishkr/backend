"""merge vendor_downloads and marketplace_policy heads

Revision ID: 817a44a3b3bb
Revises: a7d3e92f81c4, f2b7c9d4e6a1
Create Date: 2026-05-21 11:58:17.158298

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '817a44a3b3bb'
down_revision: Union[str, None] = ('a7d3e92f81c4', 'f2b7c9d4e6a1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
