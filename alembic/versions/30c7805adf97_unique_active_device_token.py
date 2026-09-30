"""add partial unique index for active device token ownership

Revision ID: 30c7805adf97
Revises: b4f8a2c1d9e7
Create Date: 2026-09-25 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '30c7805adf97'
down_revision: Union[str, None] = 'b4f8a2c1d9e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enforce one active owner per device token across all accounts."""
    op.create_index(
        "uq_device_registrations_active_token",
        "device_registrations",
        ["token"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    """Revert active token partial unique index."""
    op.drop_index("uq_device_registrations_active_token", table_name="device_registrations")
