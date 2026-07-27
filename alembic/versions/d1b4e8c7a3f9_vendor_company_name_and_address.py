"""vendor company_name + company_address

Adds the legal entity fields alongside the existing brand/display `name`.

Revision ID: d1b4e8c7a3f9
Revises: c9a1f7e2d3b8
Create Date: 2026-06-29 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1b4e8c7a3f9"
down_revision: Union[str, None] = "c9a1f7e2d3b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("company_name", sa.String(length=160), nullable=True))
    op.add_column("vendors", sa.Column("company_address", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vendors", "company_address")
    op.drop_column("vendors", "company_name")
