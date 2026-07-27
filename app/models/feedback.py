from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Feedback(Base):
    """User-submitted feedback from the website or vendor app.

    Open inbox — no consent gate (no PII required), but we capture optional
    name/email if the user wants a reply. Admin triages from here.
    """

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    category: Mapped[str] = mapped_column(String(60), index=True)
    rating: Mapped[int | None] = mapped_column(Integer)  # 1..5, optional
    message: Mapped[str] = mapped_column(Text)
    # "website" | "vendor_app" | "android" | "ios" etc. Free-text but capped.
    source: Mapped[str | None] = mapped_column(String(40), index=True)
    # Optional context — version of the app the feedback came from, etc.
    app_version: Mapped[str | None] = mapped_column(String(40))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip: Mapped[str | None] = mapped_column(String(45))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | triaged | resolved | spam
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
