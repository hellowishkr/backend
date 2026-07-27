"""Local mirror of partner catalogues (Hubble today, Xoxoday + others later).

Populated by `sync_gift_catalog` nightly. Read by `HubbleSource` and
`suggest_marketplace_gift_card` so the bot never blocks on a live partner
API during a WhatsApp turn.

Order placement (Razorpay webhook → Hubble create_order) still calls the
live partner API — never trust cached prices for money movement.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GiftCatalogItem(Base):
    __tablename__ = "gift_catalog"
    __table_args__ = (
        UniqueConstraint("partner", "external_id", name="uq_gift_catalog_partner_ext"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    partner: Mapped[str] = mapped_column(String(20), index=True)  # hubble, xoxoday, local
    external_id: Mapped[str] = mapped_column(String(80), index=True)
    brand_name: Mapped[str] = mapped_column(String(200), index=True)
    brand_name_lower: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fulfillment_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fulfillment_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    delivery_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_lead_time_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_health: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reliability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_marketplace: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
