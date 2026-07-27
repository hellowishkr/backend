from datetime import datetime

from sqlalchemy import String, Boolean, Text, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VendorLead(Base):
    __tablename__ = "vendor_leads"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    brand_name: Mapped[str] = mapped_column(String(120))
    contact_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    website: Mapped[str | None] = mapped_column(String(255))
    instagram: Mapped[str | None] = mapped_column(String(100))
    categories: Mapped[list[str]] = mapped_column(JSON)
    cities_raw: Mapped[str] = mapped_column(Text)
    # Two-letter ISO 3166-1 code, or "EU" / "OTHER". Captured on the website
    # form to drive currency/tax pre-fill in the vendor app and to segment leads.
    country: Mapped[str | None] = mapped_column(String(8), index=True)
    price_range: Mapped[str | None] = mapped_column(String(60))
    monthly_capacity: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    consent_given: Mapped[bool] = mapped_column(Boolean)
    consent_version: Mapped[str | None] = mapped_column(String(40))
    consent_text_hash: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | contacted | approved | rejected
    # Denormalized from the latest lead_activities row for this lead — lets the
    # Kanban view sort/badge by follow-up due date without an N+1 fetch.
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft delete: hidden from the admin board but kept (with its timeline)
    # for records / undo. NULL = active.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
