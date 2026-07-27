from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order


class OrderPaymentLink(Base):
    """A top-up Razorpay payment link for a delta owed after a post-purchase
    order edit increased the total. Deliberately separate from
    Order.razorpay_payment_id/razorpay_order_id, which anchor the ORIGINAL
    payment webhook's idempotency — reusing those fields for a top-up would
    let a top-up webhook collide with the original payment-capture flow.
    Supports multiple top-ups per order.
    """

    __tablename__ = "order_payment_links"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(255))
    # creating | claiming | created | cancel_pending | failed | paid |
    # cancelled | expired — an async multi-phase state machine:
    #   'creating' is inserted immediately (cheap, no external call).
    #   'claiming' is a transient state a caller moves a row into via a
    #     conditional UPDATE ... WHERE status='creating' before calling
    #     Razorpay — only one concurrent caller's UPDATE can affect the row,
    #     so this (not a DB row lock held across the HTTP call, and not
    #     timing) is what prevents two callers both calling Razorpay for the
    #     same row. Lands on 'created' or 'failed' afterward.
    # A partial unique index (uq_order_active_payment_link, WHERE status IN
    # ('creating','created','cancel_pending')) is the guard preventing two
    # ACTIVE links per order — cancel_pending is included so a replacement
    # link can't be minted until the old one's cancellation is confirmed
    # terminal (cancelled/expired/failed), not merely requested.
    status: Mapped[str] = mapped_column(String(20), default="created", server_default="created")
    razorpay_link_id: Mapped[str | None] = mapped_column(String(50))
    razorpay_short_url: Mapped[str | None] = mapped_column(String(255))
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
    # Retry bookkeeping for the reconciliation job (app/jobs/topup_link_reconciliation.py).
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Updated on every status transition — retry backoff is measured from
    # this, not created_at, since a row idle in one state for a long time
    # (e.g. 'created' for an hour) shouldn't look "immediately overdue" the
    # moment it transitions to a new state (e.g. 'cancel_pending').
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped["Order"] = relationship()
