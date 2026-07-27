"""add pricing_settings

Revision ID: b3f8a1d47c60
Revises: 1c9b42ef8d5a
Create Date: 2026-07-17

Single-row store for the storefront markup + flash-discount rates. Seeds the
program DISABLED (0/0/off) so the customer is charged the raw vendor price and
storefront display and charge stay in sync. Turn the campaign on (e.g. 30% markup
+ 20% flash) via PUT /admin/settings/pricing only once the web storefront shows
the marked-up/flash prices — otherwise checkout would display less than Razorpay
charges. The 90/10 vendor split is unaffected either way.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3f8a1d47c60"
down_revision: Union[str, None] = "1c9b42ef8d5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pricing_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("markup_pct", sa.Numeric(5, 2), nullable=False, server_default="30"),
        sa.Column("flash_off_pct", sa.Numeric(5, 2), nullable=False, server_default="20"),
        sa.Column("flash_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed DISABLED: raw vendor price, no uplift, until the storefront UI can
    # display the marked-up/flash prices consistently with what is charged.
    op.execute(
        "INSERT INTO pricing_settings (id, markup_pct, flash_off_pct, flash_active) "
        "VALUES (1, 0, 0, false)"
    )


def downgrade() -> None:
    op.drop_table("pricing_settings")
