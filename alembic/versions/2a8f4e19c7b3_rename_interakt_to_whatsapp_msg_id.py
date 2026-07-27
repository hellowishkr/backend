"""rename reminders.interakt_msg_id -> whatsapp_msg_id

We migrated off Interakt to direct Meta Cloud API, so the column name no
longer reflects reality. Data is preserved — this is a metadata-only
``ALTER COLUMN RENAME`` in Postgres with no table rewrite.

Revision ID: 2a8f4e19c7b3
Revises: 1f2a6d8b9c33
Create Date: 2026-04-23
"""

from typing import Union

from alembic import op

revision: str = "2a8f4e19c7b3"
down_revision: Union[str, None] = "1f2a6d8b9c33"
branch_labels: Union[str, tuple[str, ...], None] = None
depends_on: Union[str, tuple[str, ...], None] = None


def upgrade() -> None:
    op.alter_column(
        "reminders",
        "interakt_msg_id",
        new_column_name="whatsapp_msg_id",
    )


def downgrade() -> None:
    op.alter_column(
        "reminders",
        "whatsapp_msg_id",
        new_column_name="interakt_msg_id",
    )
