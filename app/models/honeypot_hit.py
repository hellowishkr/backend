"""Abuse-analytics record for honeypot trips on public endpoints.

Bots filling the hidden ``website_url`` field on /partners/vendor-leads land
here. Pre-launch volume is low; this table is small enough to triage in SQL
and gives a primitive feedback loop for tightening the rate limiter, the
honeypot, or the form itself if a campaign repeatedly targets us.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HoneypotHit(Base):
    __tablename__ = "honeypot_hits"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(45))  # IPv6-wide
    user_agent: Mapped[str | None] = mapped_column(String(500))
    email: Mapped[str | None] = mapped_column(String(255))
    brand_name: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
