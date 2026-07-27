"""vendor shipping_extra_item_fee_paise

Per-vendor incremental shipping charged for each unit beyond the first. Order
shipping = highest product shipping fee + this × (total units − 1), so a single
parcel is never charged double but a heavier multi-item order costs a bit more.

Revision ID: b7e2d4f1a9c3
Revises: a3f1c9d27e64
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2d4f1a9c3"
down_revision: Union[str, None] = "a3f1c9d27e64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vendors",
        sa.Column(
            "shipping_extra_item_fee_paise",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("vendors", "shipping_extra_item_fee_paise")
