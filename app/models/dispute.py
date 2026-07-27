import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, DateTime, Numeric, Text, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DisputeStatus(str, enum.Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    WON = "WON"
    LOST = "LOST"
    CLOSED = "CLOSED"


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    # RESTRICT, not CASCADE: a chargeback / dispute is a financial record
    # that must outlive any cleanup of the order. Deleting an order with an
    # open dispute should fail loudly so admin reconciles before deletion.
    order_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(50), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    reason_code: Mapped[str | None] = mapped_column(String(50))
    reason_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus), default=DisputeStatus.OPEN, nullable=False
    )
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    respond_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
