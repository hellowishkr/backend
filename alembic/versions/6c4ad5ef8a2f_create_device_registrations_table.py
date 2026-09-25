"""create device_registrations table

Revision ID: 6c4ad5ef8a2f
Revises: a7c4e2b91d60
Create Date: 2026-09-25 13:53:49.697120

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c4ad5ef8a2f'
down_revision: Union[str, None] = 'a7c4e2b91d60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create device_registrations table with indexes and cascade delete."""
    op.create_table(
        "device_registrations",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("user_id", sa.String(length=30), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False, server_default="android"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_registrations_user_id",
        "device_registrations",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_device_registrations_user_token",
        "device_registrations",
        ["user_id", "token"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema: drop indexes and device_registrations table."""
    op.drop_index("ix_device_registrations_user_token", table_name="device_registrations")
    op.drop_index("ix_device_registrations_user_id", table_name="device_registrations")
    op.drop_table("device_registrations")
