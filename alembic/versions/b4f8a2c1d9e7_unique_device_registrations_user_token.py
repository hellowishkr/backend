"""make device_registrations (user_id, token) unique

Revision ID: b4f8a2c1d9e7
Revises: 6c4ad5ef8a2f
Create Date: 2026-09-25 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4f8a2c1d9e7'
down_revision: Union[str, None] = '6c4ad5ef8a2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enforce uniqueness on (user_id, token) to prevent duplicate device registrations."""
    # Drop non-unique index
    op.drop_index("ix_device_registrations_user_token", table_name="device_registrations")
    # Create unique constraint
    op.create_unique_constraint(
        "uq_device_registrations_user_token",
        "device_registrations",
        ["user_id", "token"],
    )


def downgrade() -> None:
    """Revert unique constraint to non-unique index."""
    op.drop_constraint("uq_device_registrations_user_token", "device_registrations", type_="unique")
    op.create_index(
        "ix_device_registrations_user_token",
        "device_registrations",
        ["user_id", "token"],
        unique=False,
    )

