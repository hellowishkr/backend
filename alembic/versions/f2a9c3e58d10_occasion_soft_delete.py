"""occasion soft-delete

Revision ID: f2a9c3e58d10
Revises: b3f8a1d47c60
Create Date: 2026-07-17

Adds occasions.deleted_at so an owner can delete an occasion that still has
outstanding friend reminder-links without cascading away the celebrant_occasion
+ celebrant_friends. The row survives (hidden from owner-facing lists) so
already-sent friend links keep resolving in get_celebrant_context.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a9c3e58d10"
down_revision: Union[str, None] = "b3f8a1d47c60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "occasions",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("occasions", "deleted_at")
