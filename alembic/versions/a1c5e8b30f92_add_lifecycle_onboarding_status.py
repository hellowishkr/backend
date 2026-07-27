"""add product lifecycle_status + vendor onboarding_status

Revision ID: a1c5e8b30f92
Revises: c8f2a1d4e9b6
Create Date: 2026-06-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1c5e8b30f92"
down_revision: Union[str, None] = "c8f2a1d4e9b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Product lifecycle: draft | active | paused | archived.
    op.add_column(
        "products",
        sa.Column(
            "lifecycle_status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index(
        "ix_products_lifecycle_status", "products", ["lifecycle_status"]
    )
    # Backfill from the existing boolean so current data is consistent:
    # active products -> "active", inactive -> "paused" (not archived, so they
    # remain easily re-activatable).
    op.execute(
        "UPDATE products SET lifecycle_status = "
        "CASE WHEN active THEN 'active' ELSE 'paused' END"
    )

    # Vendor onboarding pipeline.
    op.add_column(
        "vendors",
        sa.Column(
            "onboarding_status",
            sa.String(length=20),
            nullable=False,
            server_default="live",
        ),
    )
    op.create_index(
        "ix_vendors_onboarding_status", "vendors", ["onboarding_status"]
    )
    # Existing vendors are assumed live unless inactive.
    op.execute(
        "UPDATE vendors SET onboarding_status = "
        "CASE WHEN active THEN 'live' ELSE 'paused' END"
    )


def downgrade() -> None:
    op.drop_index("ix_vendors_onboarding_status", table_name="vendors")
    op.drop_column("vendors", "onboarding_status")
    op.drop_index("ix_products_lifecycle_status", table_name="products")
    op.drop_column("products", "lifecycle_status")
