"""product.ships_nationally + city.pincode_prefixes

Revision ID: d3a9f1c7b502
Revises: c8f1a4b3d960
Create Date: 2026-06-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3a9f1c7b502"
down_revision: Union[str, None] = "c8f1a4b3d960"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Default true: existing products keep current "don't block" behaviour;
    # vendors opt specific products into local-only.
    op.add_column(
        "products",
        sa.Column(
            "ships_nationally",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )
    op.add_column(
        "cities",
        sa.Column("pincode_prefixes", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cities", "pincode_prefixes")
    op.drop_column("products", "ships_nationally")
