from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CorporateLead(Base):
    __tablename__ = "corporate_leads"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(160))
    contact_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    role: Mapped[str | None] = mapped_column(String(80))
    recipient_count: Mapped[int | None] = mapped_column(Integer)
    occasion: Mapped[str | None] = mapped_column(String(80))
    budget_per_recipient: Mapped[str | None] = mapped_column(String(80))
    timeline: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    # single | multi-address — how the order should be shipped to recipients.
    delivery_type: Mapped[str | None] = mapped_column(String(20))
    needed_by: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    # Denormalized from the latest lead_activities row for this lead — lets the
    # Kanban view sort/badge by follow-up due date without an N+1 fetch.
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Soft delete: hidden from the admin board but kept (with its timeline)
    # for records / undo. NULL = active.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
