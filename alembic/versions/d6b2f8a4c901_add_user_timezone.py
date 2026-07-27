"""add user timezone

Revision ID: d6b2f8a4c901
Revises: c5a1e7d9b304
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6b2f8a4c901"
down_revision: Union[str, None] = "c5a1e7d9b304"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="Asia/Kolkata",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "timezone")
