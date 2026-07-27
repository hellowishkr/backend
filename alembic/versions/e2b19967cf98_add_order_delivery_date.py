"""add order delivery date

Revision ID: e2b19967cf98
Revises: b1d8f3a6c2e9
Create Date: 2026-06-21 09:34:20.549994

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e2b19967cf98'
down_revision: Union[str, None] = 'b1d8f3a6c2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('delivery_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'delivery_date')
