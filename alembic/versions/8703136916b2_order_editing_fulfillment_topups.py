"""order line-item edits: fulfillment tracking fields + order_payment_links table

Adds tracking_number/carrier/tracking_url and amount_collected_paise to
orders (fulfillment_status keeps its existing plain-string column — no DB
constraint change, only the Pydantic regex in admin_orders.py is extended to
pending|shipped|delivered) and a new order_payment_links table for tracking
top-up Razorpay payment links separately from the order's original
razorpay_payment_id/razorpay_order_id.

Revision ID: 8703136916b2
Revises: d1e5a9c2b647
Create Date: 2026-07-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8703136916b2"
down_revision: Union[str, None] = "d1e5a9c2b647"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("tracking_number", sa.String(length=100), nullable=True))
    op.add_column("orders", sa.Column("tracking_carrier", sa.String(length=80), nullable=True))
    op.add_column("orders", sa.Column("tracking_url", sa.String(length=500), nullable=True))
    op.add_column("orders", sa.Column("amount_collected_paise", sa.Integer(), nullable=True))

    # fulfillment_status moves from a pending|fulfilled binary to
    # pending|shipped|delivered. Existing "fulfilled" rows would otherwise
    # hold a value the new API rejects and match neither new filter.
    op.execute("UPDATE orders SET fulfillment_status = 'delivered' WHERE fulfillment_status = 'fulfilled'")

    op.create_table(
        "order_payment_links",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(length=30),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="created"),
        sa.Column("razorpay_link_id", sa.String(length=50), nullable=True),
        sa.Column("razorpay_short_url", sa.String(length=255), nullable=True),
        sa.Column("razorpay_payment_id", sa.String(length=50), nullable=True, unique=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_order_payment_links_order_id", "order_payment_links", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_order_payment_links_order_id", table_name="order_payment_links")
    op.drop_table("order_payment_links")
    op.drop_column("orders", "amount_collected_paise")
    op.drop_column("orders", "tracking_url")
    op.drop_column("orders", "tracking_carrier")
    op.drop_column("orders", "tracking_number")
    # The reverted app only understands fulfillment_status = pending|fulfilled
    # (its FulfillmentPayload regex rejects anything else). Collapse both new
    # states back to the closest equivalent under that model so downgraded
    # rows stay usable, even though shipped-vs-delivered distinction is lost.
    op.execute(
        "UPDATE orders SET fulfillment_status = 'fulfilled' "
        "WHERE fulfillment_status IN ('shipped', 'delivered')"
    )
