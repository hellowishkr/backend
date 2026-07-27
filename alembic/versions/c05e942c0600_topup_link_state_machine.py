"""top-up payment link async state machine

Adds creating|cancel_pending|failed to order_payment_links.status (alongside
the existing created|paid|cancelled|expired) and a partial unique index that
is the actual source of truth preventing two concurrently-active (creating
or created) links on the same order — replacing the SELECT...FOR UPDATE
order-row-lock strategy in app/api/admin_orders.py, which previously held a
Postgres lock across synchronous Razorpay HTTP calls.

Revision ID: c05e942c0600
Revises: 8703136916b2
Create Date: 2026-07-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c05e942c0600"
down_revision: Union[str, None] = "8703136916b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_payment_links",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "order_payment_links", sa.Column("last_error", sa.String(length=500), nullable=True)
    )

    # Defensive pre-flight: today's flow already tries to keep at most one
    # "created" row per order, so this should be a no-op in practice, but a
    # migration that assumes it rather than verifying it is how partial
    # unique index creation fails loudly mid-deploy. Any order with more
    # than one "created" row keeps only the most recent; the rest are
    # cancelled (matches what create_topup_link already does at runtime).
    op.execute(
        """
        UPDATE order_payment_links
        SET status = 'cancelled'
        WHERE status = 'created'
          AND id NOT IN (
            SELECT DISTINCT ON (order_id) id
            FROM order_payment_links
            WHERE status = 'created'
            ORDER BY order_id, created_at DESC
          )
        """
    )

    # Partial unique index — the actual concurrency guard. A second INSERT
    # racing to create a 'creating' row for the same order_id while one is
    # already creating/created fails with a unique-violation, which
    # create_topup_link catches and turns into a 409.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_order_active_payment_link
        ON order_payment_links(order_id)
        WHERE status IN ('creating', 'created')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_order_active_payment_link")
    op.drop_column("order_payment_links", "last_error")
    op.drop_column("order_payment_links", "retry_count")
