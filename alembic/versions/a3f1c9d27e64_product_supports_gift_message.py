"""product supports_gift_message

Per-product flag for whether the vendor accepts a gift message. The storefront
only shows the gift-message field when this is set.

Revision ID: a3f1c9d27e64
Revises: d1b4e8c7a3f9
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f1c9d27e64"
down_revision: Union[str, None] = "d1b4e8c7a3f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "supports_gift_message",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "supports_gift_message")
