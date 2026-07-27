"""Recipient gift preference models.

RecipientPreference — structured gift preferences for a person.
PreferenceRequest   — a gift-giver's request to collect preferences from a recipient.

Design notes:
- Preferences are stored per phone (not user_id) so they work even before
  the recipient registers on Wishkr.
- PreferenceRequest tracks state so we can: deduplicate, notify the
  gift-giver when the recipient responds, and prevent spam.
- Purchaser identity and request origin are never exposed to the recipient.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RecipientPreference(Base):
    """Structured gift preferences for a recipient, keyed by phone.

    Populated either by the recipient directly or by parsing a WhatsApp reply
    to a preference-request message.
    """
    __tablename__ = "recipient_preferences"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    # Phone-keyed so it works before the recipient has a Wishkr account.
    recipient_phone: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    # Comma-separated canonical vibe IDs (e.g. "gadgets,coffee_lover,book_lover")
    # or free-text categories if we couldn't resolve to canonical IDs.
    preferred_categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    disliked_categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_brands: Mapped[str | None] = mapped_column(Text, nullable=True)
    disliked_brands: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Budget hint extracted from the recipient's message.
    preferred_budget_min: Mapped[int | None] = mapped_column(nullable=True)
    preferred_budget_max: Mapped[int | None] = mapped_column(nullable=True)
    # Raw notes / free text for anything the parser couldn't structure.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # External wishlist links the recipient shared (JSON array of URLs).
    wishlist_links: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the preferences were collected.
    # 'direct_response' — recipient replied to a preference-request message.
    # 'wishlist_link'   — recipient shared an Amazon/Flipkart wishlist link.
    # 'manual'          — gift-giver entered preferences on recipient's behalf.
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="direct_response")
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PreferenceRequest(Base):
    """A gift-giver's request to collect preferences from a recipient.

    Tracks state machine:
      pending    — message sent to recipient, awaiting reply.
      responded  — recipient replied; preferences saved.
      expired    — no reply after TTL; gift-giver is notified.
      cancelled  — gift-giver or system cancelled the request.

    Deduplication: only one 'pending' request is allowed per
    (recipient_phone, occasion_id) pair. Multiple gift-givers waiting on
    the same recipient-occasion reuse the same pending request row.
    """
    __tablename__ = "preference_requests"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    recipient_phone: Mapped[str] = mapped_column(String(15), index=True)
    # The occasion this request is tied to (optional — may be contact occasion
    # id if gift-giver has it, or celebrant_occasion_id in friend flow).
    occasion_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # The gift-giver who initiated the request.
    requester_phone: Mapped[str] = mapped_column(String(15), index=True)
    # If multiple gift-givers are waiting, store them as a comma-separated
    # list of phones so we can notify all when preferences arrive.
    notify_phones: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Request state.
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="pending"
    )
    # Whether the recipient message should reveal the gift-giver's identity.
    anonymous: Mapped[bool] = mapped_column(nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # TTL — after this timestamp without response, status → expired.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_pref_req_recipient", "recipient_phone"),
        Index(
            "ix_pref_req_pending",
            "recipient_phone",
            "occasion_id",
            postgresql_where=text("status = 'pending'"),
        ),
    )
