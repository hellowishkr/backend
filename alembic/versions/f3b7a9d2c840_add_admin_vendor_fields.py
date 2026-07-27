"""add admin vendor fields

Revision ID: f3b7a9d2c840
Revises: e6b4c2a91f30
Create Date: 2026-06-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b7a9d2c840"
down_revision: Union[str, None] = "e6b4c2a91f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("gstin", sa.String(length=20), nullable=True))
    op.add_column("vendors", sa.Column("pan", sa.String(length=20), nullable=True))
    op.add_column("vendors", sa.Column("bank_account_name", sa.String(length=120), nullable=True))
    op.add_column("vendors", sa.Column("bank_account_number", sa.String(length=40), nullable=True))
    op.add_column("vendors", sa.Column("bank_ifsc", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("vendors", "bank_ifsc")
    op.drop_column("vendors", "bank_account_number")
    op.drop_column("vendors", "bank_account_name")
    op.drop_column("vendors", "pan")
    op.drop_column("vendors", "gstin")
