"""gift_catalog table — local mirror of partner catalogues

Revision ID: f77cfe4f4b1e
Revises: 2a8f4e19c7b3
Create Date: 2026-04-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f77cfe4f4b1e"
down_revision: Union[str, None] = "2a8f4e19c7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gift_catalog",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("partner", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=80), nullable=False),
        sa.Column("brand_name", sa.String(length=200), nullable=False),
        sa.Column("brand_name_lower", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("min_amount", sa.Integer(), nullable=True),
        sa.Column("max_amount", sa.Integer(), nullable=True),
        sa.Column("is_marketplace", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("partner", "external_id", name="uq_gift_catalog_partner_ext"),
    )
    op.create_index("ix_gift_catalog_partner", "gift_catalog", ["partner"])
    op.create_index("ix_gift_catalog_external_id", "gift_catalog", ["external_id"])
    op.create_index("ix_gift_catalog_brand_name", "gift_catalog", ["brand_name"])
    op.create_index("ix_gift_catalog_brand_name_lower", "gift_catalog", ["brand_name_lower"])
    op.create_index("ix_gift_catalog_category", "gift_catalog", ["category"])
    op.create_index("ix_gift_catalog_is_marketplace", "gift_catalog", ["is_marketplace"])
    op.create_index("ix_gift_catalog_active", "gift_catalog", ["active"])


def downgrade() -> None:
    op.drop_index("ix_gift_catalog_active", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_is_marketplace", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_category", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_brand_name_lower", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_brand_name", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_external_id", table_name="gift_catalog")
    op.drop_index("ix_gift_catalog_partner", table_name="gift_catalog")
    op.drop_table("gift_catalog")
