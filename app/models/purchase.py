import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, DateTime, Numeric, Text, ForeignKey, Enum, Index, Integer, JSON, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class GiftType(str, enum.Enum):
    GIFT_CARD = "GIFT_CARD"
    FLOWERS = "FLOWERS"
    CAKE = "CAKE"
    CHOCOLATES = "CHOCOLATES"
    EXPERIENCE = "EXPERIENCE"
    PRIVATE_THEATRE = "PRIVATE_THEATRE"
    PERSONALIZED = "PERSONALIZED"
    DECORATION = "DECORATION"
    OTHER = "OTHER"


class PurchaseStatus(str, enum.Enum):
    PENDING = "PENDING"
    VOUCHER_ISSUED = "VOUCHER_ISSUED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class DeliveryStatus(str, enum.Enum):
    PENDING_DELIVERY = "PENDING_DELIVERY"
    DELIVERED = "DELIVERED"
    FAILED_DELIVERY = "FAILED_DELIVERY"


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    # If the occasion is deleted (e.g. DPDP request, user cleanup) the
    # Purchase row stays so the financial / Hubble receipt is preserved;
    # losing it would orphan the user's order in our books.
    occasion_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("occasions.id", ondelete="SET NULL")
    )
    gift_type: Mapped[GiftType] = mapped_column(Enum(GiftType))
    brand_name: Mapped[str | None] = mapped_column(String(100))
    # Provider-neutral fulfillment fields. Hubble-specific columns below stay
    # for compatibility while newer partners (cakes, flowers, theatre, local
    # vendors) use provider/provider_product_id/provider_order_id.
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_product_id: Mapped[str | None] = mapped_column(String(80))
    provider_order_id: Mapped[str | None] = mapped_column(String(80))
    fulfillment_policy: Mapped[str | None] = mapped_column(String(40))
    fulfillment_lead_time_hours: Mapped[int | None] = mapped_column(Integer)
    fulfillment_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_metadata_json: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(astext_type=Text()), "postgresql")
    )
    recipient_user_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="SET NULL")
    )
    recipient_phone: Mapped[str | None] = mapped_column(String(15))
    # Soft future reference until the physical-delivery address table exists.
    delivery_address_id: Mapped[str | None] = mapped_column(String(30))
    special_instructions: Mapped[str | None] = mapped_column(Text)
    hubble_product_id: Mapped[str | None] = mapped_column(String(50))
    hubble_order_id: Mapped[str | None] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # face value (what user pays)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # what we pay (after Hubble discount)
    voucher_code: Mapped[str | None] = mapped_column(String(50))
    voucher_pin: Mapped[str | None] = mapped_column(String(20))
    voucher_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[PurchaseStatus] = mapped_column(Enum(PurchaseStatus), default=PurchaseStatus.PENDING)
    # Celebrant flow: friend → celebrant delivery
    personal_message: Mapped[str | None] = mapped_column(Text)
    buyer_phone: Mapped[str | None] = mapped_column(String(15))
    delivery_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped["DeliveryStatus | None"] = mapped_column(Enum(DeliveryStatus))
    # Optional FK to the wishlist item that motivated this purchase. When
    # set, the Razorpay completion webhook stamps gifted_at on that row
    # so suggest_gifts stops recommending the same product next year.
    wishlist_item_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("wishlist_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="purchases")
    occasion: Mapped["Occasion | None"] = relationship(back_populates="purchases")

    __table_args__ = (
        Index("ix_purchases_occasion_gift", "occasion_id", "gift_type"),
        Index("ix_purchases_provider_due", "provider", "fulfillment_due_at"),
        Index("ix_purchases_recipient_user", "recipient_user_id"),
        Index(
            "ix_purchases_wishlist_item",
            "wishlist_item_id",
            postgresql_where=text("wishlist_item_id IS NOT NULL"),
        ),
    )


from app.models.order import Order  # noqa: E402, F401
from app.models.occasion import Occasion  # noqa: E402, F401
