"""top-up payment link state machine hardening

Second-pass fixes to the async state machine introduced in c05e942c0600:

1. Widens uq_order_active_payment_link to also cover 'cancel_pending' — a
   replacement link can no longer be inserted while the old link's
   Razorpay-side cancellation is still unconfirmed, closing a window where
   two links could be simultaneously payable.
2. Adds state_changed_at, updated on every status transition, so retry
   backoff (app.jobs.topup_link_reconciliation._is_retry_due) reflects how
   long a row has actually been in its CURRENT state rather than always
   using the row's original created_at (which made a link that just
   transitioned to cancel_pending look immediately "stuck").

Revision ID: 466634165c0a
Revises: c05e942c0600
Create Date: 2026-07-02 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "466634165c0a"
down_revision: Union[str, None] = "c05e942c0600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_payment_links",
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.execute("DROP INDEX IF EXISTS uq_order_active_payment_link")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_order_active_payment_link
        ON order_payment_links(order_id)
        WHERE status IN ('creating', 'created', 'cancel_pending')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_order_active_payment_link")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_order_active_payment_link
        ON order_payment_links(order_id)
        WHERE status IN ('creating', 'created')
        """
    )
    op.drop_column("order_payment_links", "state_changed_at")
