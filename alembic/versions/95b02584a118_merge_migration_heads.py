"""merge migration heads

Revision ID: 95b02584a118
Revises: 9d2f6a8c4b71, f9d3e7a2c1b8
Create Date: 2026-05-10 18:18:04.302287

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '95b02584a118'
down_revision: Union[str, None] = ('9d2f6a8c4b71', 'f9d3e7a2c1b8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
