from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VendorAppDownload(Base):
    """A vendor who filled the gate form on /vendor-app before downloading.

    Separate from VendorLead (full partner application). This is a lightweight
    contact-capture used to follow up with vendors about how the desktop app
    is performing in the wild.
    """

    __tablename__ = "vendor_app_downloads"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    contact_name: Mapped[str] = mapped_column(String(100))
    brand_name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    city: Mapped[str | None] = mapped_column(String(120))
    platform: Mapped[str | None] = mapped_column(String(20))  # windows | macos | linux
    app_version: Mapped[str | None] = mapped_column(String(20))
    consent_given: Mapped[bool] = mapped_column(Boolean)
    consent_version: Mapped[str | None] = mapped_column(String(40))
    consent_text_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
