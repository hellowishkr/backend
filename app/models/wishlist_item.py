"""Wishlist item — a product link a user wants gifted."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WishlistItem(Base):
    __tablename__ = "wishlist_items"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # `amazon:B09XYZ1234` / `flipkart:itm…` — None when we couldn't parse one.
    product_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # 'self' = user wants it for themselves; 'gift' = saving as a gift idea
    # for someone else's occasion. Disambiguates pastes during friend-flow
    # so they don't pollute the saver's personal wishlist.
    purpose: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="self"
    )
    # Polymorphic — `occ_*` (user-tracked occasion) or `cel_*`
    # (celebrant-set occasion). Required when purpose='gift', NULL for self.
    recipient_occasion_id: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    # Personal occasion link — allows a user to organise self-wishlist items
    # by their own upcoming occasion (birthday, wedding, housewarming …).
    # Always NULL for purpose='gift'; only populated for purpose='self'.
    # Soft-FK: the value is an occ_* id owned by the same user — validated
    # at the application layer, not via a hard FK (avoids cascade headaches
    # when the occasion is deleted but items should survive).
    own_occasion_id: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    # Sharing visibility for purpose='self' items:
    #   'private'  — only owner can see (default)
    #   'friends'  — invited friends on the linked occasion can view
    #   'public'   — share-link accessible (not yet implemented in UI)
    # purpose='gift' items are always private; visibility is ignored.
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="private"
    )
    # Lifecycle: set when the item is fulfilled (Wishkr order placed or
    # external_gift_claim logged). suggest_gifts filters these out so we
    # don't recommend the same product two years running.
    gifted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gifted_order_id: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    # Reservation — set when a friend claims this item before purchase so
    # that two friends don't buy the same thing simultaneously.
    reserved_by_user_id: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    reserved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reservation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Final purchase identity — set once the reserved friend confirms purchase.
    purchased_by_user_id: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    purchased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Machine-readable status for the sharing/reservation lifecycle.
    # 'available'  — not yet reserved or purchased
    # 'reserved'   — claimed by a friend, pending purchase confirmation
    # 'purchased'  — confirmed purchased; hidden from other friends
    # 'expired'    — reservation lapsed; back to available
    share_status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="available"
    )
    # URL-staleness tracking — the validation worker pings each URL on a
    # cadence and stamps these. Suggester filters where unavailable_at IS NULL.
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unavailable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Ad-hoc date-scheduled nudge ("remind me on April 29 about this").
    # Worker picks up rows where remind_on <= today AND reminded_at IS NULL,
    # sends a WhatsApp message, then stamps reminded_at.
    remind_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Dedup is purpose-aware: same URL can coexist as 'self' AND as a
        # 'gift' for a specific friend's occasion without colliding.
        # COALESCE folds NULL recipient ids into a stable bucket so the
        # unique constraint covers self-saves too. Postgres-only.
        Index(
            "uq_wishlist_user_url",
            "user_id",
            "url",
            "purpose",
            text("COALESCE(recipient_occasion_id, '')"),
            unique=True,
        ),
        Index(
            "uq_wishlist_user_product",
            "user_id",
            "product_id",
            "purpose",
            text("COALESCE(recipient_occasion_id, '')"),
            unique=True,
            postgresql_where=text("product_id IS NOT NULL"),
        ),
        Index(
            "ix_wishlist_recipient_occasion",
            "recipient_occasion_id",
            postgresql_where=text("recipient_occasion_id IS NOT NULL"),
        ),
        Index(
            "ix_wishlist_own_occasion",
            "own_occasion_id",
            postgresql_where=text("own_occasion_id IS NOT NULL"),
        ),
        # Reserved items — worker can scan for expired reservations.
        Index(
            "ix_wishlist_reserved",
            "reserved_at",
            postgresql_where=text("reserved_by_user_id IS NOT NULL AND purchased_at IS NULL"),
        ),
        # Worker-query indexes — kept partial so unset rows don't bloat them.
        Index(
            "ix_wishlist_remind_pending",
            "remind_on",
            postgresql_where=text(
                "remind_on IS NOT NULL AND reminded_at IS NULL"
            ),
        ),
        Index(
            "ix_wishlist_validation_due",
            "last_validated_at",
            postgresql_where=text("unavailable_at IS NULL"),
        ),
    )
