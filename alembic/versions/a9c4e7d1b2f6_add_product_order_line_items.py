"""add product order line items

Revision ID: a9c4e7d1b2f6
Revises: f3b7a9d2c840
Create Date: 2026-06-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9c4e7d1b2f6"
down_revision: Union[str, None] = "f3b7a9d2c840"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("order_type", sa.String(length=20), nullable=True))
    op.execute("UPDATE orders SET order_type = 'voucher' WHERE order_type IS NULL")
    op.alter_column("orders", "order_type", nullable=False)
    op.create_index(op.f("ix_orders_order_type"), "orders", ["order_type"], unique=False)

    op.add_column("orders", sa.Column("vendor_id", sa.String(length=30), nullable=True))
    op.add_column("orders", sa.Column("vendor_amount_paise", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("platform_amount_paise", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("razorpay_order_id", sa.String(length=50), nullable=True))
    op.add_column("orders", sa.Column("shipping_name", sa.String(length=120), nullable=True))
    op.add_column("orders", sa.Column("shipping_phone", sa.String(length=20), nullable=True))
    op.add_column("orders", sa.Column("shipping_line1", sa.String(length=200), nullable=True))
    op.add_column("orders", sa.Column("shipping_line2", sa.String(length=200), nullable=True))
    op.add_column("orders", sa.Column("shipping_city", sa.String(length=80), nullable=True))
    op.add_column("orders", sa.Column("shipping_state", sa.String(length=80), nullable=True))
    op.add_column("orders", sa.Column("shipping_pincode", sa.String(length=20), nullable=True))
    op.create_foreign_key(
        "fk_orders_vendor_id_vendors",
        "orders",
        "vendors",
        ["vendor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_orders_vendor_id"), "orders", ["vendor_id"], unique=False)
    op.create_index(op.f("ix_orders_razorpay_order_id"), "orders", ["razorpay_order_id"], unique=False)

    op.create_table(
        "order_line_items",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("order_id", sa.String(length=30), nullable=False),
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.String(length=30), nullable=False),
        sa.Column("variant_id", sa.String(length=30), nullable=True),
        sa.Column("addon_ids", sa.JSON(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("unit_price_paise", sa.Integer(), nullable=False),
        sa.Column("line_total_paise", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_order_line_items_order_id"), "order_line_items", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_line_items_vendor_id"), "order_line_items", ["vendor_id"], unique=False)
    op.create_index(op.f("ix_order_line_items_product_id"), "order_line_items", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_order_line_items_product_id"), table_name="order_line_items")
    op.drop_index(op.f("ix_order_line_items_vendor_id"), table_name="order_line_items")
    op.drop_index(op.f("ix_order_line_items_order_id"), table_name="order_line_items")
    op.drop_table("order_line_items")

    op.drop_index(op.f("ix_orders_razorpay_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_vendor_id"), table_name="orders")
    op.drop_constraint("fk_orders_vendor_id_vendors", "orders", type_="foreignkey")
    op.drop_column("orders", "shipping_pincode")
    op.drop_column("orders", "shipping_state")
    op.drop_column("orders", "shipping_city")
    op.drop_column("orders", "shipping_line2")
    op.drop_column("orders", "shipping_line1")
    op.drop_column("orders", "shipping_phone")
    op.drop_column("orders", "shipping_name")
    op.drop_column("orders", "razorpay_order_id")
    op.drop_column("orders", "platform_amount_paise")
    op.drop_column("orders", "vendor_amount_paise")
    op.drop_column("orders", "vendor_id")
    op.drop_index(op.f("ix_orders_order_type"), table_name="orders")
    op.drop_column("orders", "order_type")
