import enum
import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, DateTime, Date, Numeric, ForeignKey, Enum, Integer, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order_line_item import OrderLineItem
    from app.models.purchase import Purchase
    from app.models.user import User


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"          # payment link sent
    PAID = "PAID"                # payment received
    PROCESSING = "PROCESSING"    # voucher being issued
    COMPLETED = "COMPLETED"      # voucher delivered
    FAILED = "FAILED"            # payment or voucher failed
    EXPIRED = "EXPIRED"          # payment link expired
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    REFUND_FAILED = "REFUND_FAILED"      # refund initiated by us but bank/UPI rejected it
    PARTIALLY_PAID = "PARTIALLY_PAID"    # customer paid less than link amount
    DISPUTED = "DISPUTED"                # chargeback filed
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    # High-entropy (256-bit) capability token for the public order-confirmation
    # page. The order id alone is NOT sufficient authorisation — it's only a
    # timestamp + 32 random bits (see app.utils.id_gen), guessable/scannable
    # for an id whose creation time is roughly known. This token is the actual
    # credential; it's handed to the buyer once (checkout redirect) and must
    # be supplied on every subsequent confirmation-page load.
    confirmation_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=lambda: secrets.token_urlsafe(32)
    )
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))   # face value + fee + gst
    gift_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)  # face value only
    platform_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    platform_fee_gst: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    order_type: Mapped[str] = mapped_column(String(20), default="voucher", index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(30), ForeignKey("vendors.id", ondelete="SET NULL"), index=True)
    vendor_amount_paise: Mapped[int | None] = mapped_column(Integer)
    platform_amount_paise: Mapped[int | None] = mapped_column(Integer)
    razorpay_link_id: Mapped[str | None] = mapped_column(String(50))
    razorpay_order_id: Mapped[str | None] = mapped_column(String(50), index=True)
    razorpay_short_url: Mapped[str | None] = mapped_column(String(255))
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    shipping_name: Mapped[str | None] = mapped_column(String(120))
    shipping_phone: Mapped[str | None] = mapped_column(String(20))
    shipping_line1: Mapped[str | None] = mapped_column(String(200))
    shipping_line2: Mapped[str | None] = mapped_column(String(200))
    shipping_city: Mapped[str | None] = mapped_column(String(80))
    shipping_state: Mapped[str | None] = mapped_column(String(80))
    shipping_pincode: Mapped[str | None] = mapped_column(String(20))
    delivery_date: Mapped[date | None] = mapped_column(Date)
    gift_message: Mapped[str | None] = mapped_column(Text)
    is_gift: Mapped[bool] = mapped_column(Boolean, default=True)
    hide_prices: Mapped[bool] = mapped_column(Boolean, default=True)
    # First/last campaign touches captured on the storefront. This deliberately
    # excludes names, phone numbers, addresses, and gift-message content.
    marketing_attribution: Mapped[dict | None] = mapped_column(JSON)
    tracking_number: Mapped[str | None] = mapped_column(String(100))
    tracking_carrier: Mapped[str | None] = mapped_column(String(80))
    tracking_url: Mapped[str | None] = mapped_column(String(500))
    # Amount actually collected via the ORIGINAL payment, frozen at the moment
    # razorpay_payment_id is first claimed. total_amount is live and can be
    # mutated by post-purchase edits, so this is the only reliable anchor for
    # computing an outstanding balance after an edit raises the total.
    amount_collected_paise: Mapped[int | None] = mapped_column(Integer)
    # Fulfilment tracking for product orders (admin panel). Payment status lives
    # in `status`; fulfilment is a separate axis: pending | shipped | delivered.
    fulfillment_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    # Set when the vendor WhatsApp notification was last sent (best-effort).
    vendor_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="orders")
    purchases: Mapped[list["Purchase"]] = relationship(back_populates="order")
    line_items: Mapped[list["OrderLineItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


from app.models.user import User  # noqa: E402, F401
from app.models.purchase import Purchase  # noqa: E402, F401
