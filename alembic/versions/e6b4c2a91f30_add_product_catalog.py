"""add product catalog

Revision ID: e6b4c2a91f30
Revises: d4e8f1a92c63
Create Date: 2026-06-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6b4c2a91f30"
down_revision: Union[str, None] = "d4e8f1a92c63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("razorpay_account_id", sa.String(length=80), nullable=True))

    op.create_table(
        "products",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_price_paise", sa.Integer(), nullable=False),
        sa.Column("gst_rate", sa.Numeric(5, 2), nullable=True),
        sa.Column("hsn", sa.String(length=20), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_products_active"), "products", ["active"], unique=False)
    op.create_index(op.f("ix_products_vendor_id"), "products", ["vendor_id"], unique=False)

    op.create_table(
        "product_variants",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sku", sa.String(length=80), nullable=True),
        sa.Column("price_delta_paise", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "name", name="uq_product_variants_product_name"),
        sa.UniqueConstraint("sku"),
    )
    op.create_index(op.f("ix_product_variants_active"), "product_variants", ["active"], unique=False)
    op.create_index(op.f("ix_product_variants_product_id"), "product_variants", ["product_id"], unique=False)
    op.create_index(op.f("ix_product_variants_sku"), "product_variants", ["sku"], unique=False)

    op.create_table(
        "product_addons",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("price_paise", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "name", name="uq_product_addons_product_name"),
    )
    op.create_index(op.f("ix_product_addons_active"), "product_addons", ["active"], unique=False)
    op.create_index(op.f("ix_product_addons_product_id"), "product_addons", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_product_addons_product_id"), table_name="product_addons")
    op.drop_index(op.f("ix_product_addons_active"), table_name="product_addons")
    op.drop_table("product_addons")

    op.drop_index(op.f("ix_product_variants_sku"), table_name="product_variants")
    op.drop_index(op.f("ix_product_variants_product_id"), table_name="product_variants")
    op.drop_index(op.f("ix_product_variants_active"), table_name="product_variants")
    op.drop_table("product_variants")

    op.drop_index(op.f("ix_products_vendor_id"), table_name="products")
    op.drop_index(op.f("ix_products_active"), table_name="products")
    op.drop_table("products")

    op.drop_column("vendors", "razorpay_account_id")
