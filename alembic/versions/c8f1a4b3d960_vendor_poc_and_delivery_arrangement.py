"""add vendor poc_name, poc_email, delivery_arrangement

Revision ID: c8f1a4b3d960
Revises: b2d6f1a40c73
Create Date: 2026-06-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8f1a4b3d960"
# Two heads existed on disk (b2d6f1a40c73 order-fulfillment + a1d4f7b92c63
# vendor-documents). Chain off both so this revision also merges them.
down_revision: Union[str, tuple[str, ...], None] = ("b2d6f1a40c73", "a1d4f7b92c63")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("poc_name", sa.String(length=120), nullable=True))
    op.add_column("vendors", sa.Column("poc_email", sa.String(length=160), nullable=True))
    op.add_column(
        "vendors",
        sa.Column(
            "delivery_arrangement",
            sa.String(length=20),
            nullable=False,
            server_default="self",
        ),
    )


def downgrade() -> None:
    op.drop_column("vendors", "delivery_arrangement")
    op.drop_column("vendors", "poc_email")
    op.drop_column("vendors", "poc_name")
