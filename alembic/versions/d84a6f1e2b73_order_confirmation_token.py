"""orders.confirmation_token

The public order-confirmation page (GET /api/orders/product/{order_id})
returns phone, shipping address, gift message, and tracking info, but was
authorised by the order id alone. Order ids are only a unix timestamp plus
32 random bits (app.utils.id_gen), and an order's approximate creation time
is knowable — nowhere near 128 bits of real entropy against a targeted scan.

confirmation_token is a separate, 256-bit-random capability token: minted at
order creation, handed to the buyer once via the checkout redirect, and
required as a query param on every confirmation-page load. The order id
remains a public-ish identifier; this token is the actual credential.

Revision ID: d84a6f1e2b73
Revises: c7e1f92b4a06
Create Date: 2026-07-05 00:00:00.000000
"""

import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d84a6f1e2b73"
down_revision: Union[str, None] = "c7e1f92b4a06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("confirmation_token", sa.String(length=64), nullable=True),
    )

    # Backfill existing rows with a random token each (can't be done in pure
    # SQL — needs Python's secrets — so page through rows in a loop).
    connection = op.get_bind()
    orders_table = sa.table(
        "orders", sa.column("id", sa.String), sa.column("confirmation_token", sa.String)
    )
    result = connection.execute(sa.text("SELECT id FROM orders WHERE confirmation_token IS NULL"))
    for (order_id,) in result:
        connection.execute(
            orders_table.update()
            .where(orders_table.c.id == order_id)
            .values(confirmation_token=secrets.token_urlsafe(32))
        )

    op.alter_column("orders", "confirmation_token", nullable=False)
    # Matches Mapped[str] = mapped_column(..., unique=True, index=True) on the
    # Order model: a single unique index, not a separate constraint + index.
    op.create_index(
        "ix_orders_confirmation_token", "orders", ["confirmation_token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_orders_confirmation_token", table_name="orders")
    op.drop_column("orders", "confirmation_token")
