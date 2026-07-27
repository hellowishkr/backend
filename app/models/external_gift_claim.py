"""External gift claim — friend self-reports a gift bought outside Wishkr.

Lets us track the full gift checklist for an occasion even when the buyer
purchased elsewhere (Amazon, direct from brand, etc). Used for dedup so
other friends don't duplicate the gift.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExternalGiftClaim(Base):
    __tablename__ = "external_gift_claims"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    occasion_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("occasions.id", ondelete="CASCADE"), index=True
    )
    buyer_phone: Mapped[str] = mapped_column(String(15), index=True)
    item_description: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(50))
    brand_name: Mapped[str | None] = mapped_column(String(100))
    # Optional FK back to the wishlist item this claim fulfilled. Closes the
    # dedupe loop: when set, suggest_gifts knows the URL is already taken
    # without needing fuzzy text matching against item_description.
    wishlist_item_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("wishlist_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_external_gift_claims_occasion", "occasion_id"),
        Index(
            "ix_claims_wishlist_item",
            "wishlist_item_id",
            postgresql_where=text("wishlist_item_id IS NOT NULL"),
        ),
    )
