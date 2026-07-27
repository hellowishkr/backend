"""add order fulfillment_status + vendor_notified_at

Revision ID: b2d6f1a40c73
Revises: a1c5e8b30f92
Create Date: 2026-06-21 00:00:00.000000

If `alembic heads` shows more than one head before applying, merge first:
    alembic merge -m "merge heads" <head1> <head2> ...
then set down_revision below to that merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d6f1a40c73"
down_revision: Union[str, None] = "a1c5e8b30f92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "fulfillment_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index(
        "ix_orders_fulfillment_status", "orders", ["fulfillment_status"]
    )
    op.add_column(
        "orders",
        sa.Column("vendor_notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "vendor_notified_at")
    op.drop_index("ix_orders_fulfillment_status", table_name="orders")
    op.drop_column("orders", "fulfillment_status")
