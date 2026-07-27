from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class GiftLog(Base):
    """A gift the user gave someone, logged for memory across occasions.

    Distinct from ``Purchase`` (which is order/payment-coupled): this captures
    casual off-platform gifts the user tells the bot about ("I gave Riya a
    watch last year") so future suggestions can avoid repeats. No money, no
    fulfillment — just history.
    """

    __tablename__ = "gift_log"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Contact may be deleted independently; keep the log row so the user's
    # gifting history survives. Occasion is optional — a gift need not be tied
    # to a saved occasion.
    contact_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    occasion_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("occasions.id", ondelete="SET NULL")
    )
    description: Mapped[str] = mapped_column(String(200))
    # Four-digit year the gift was given, when known (free-text "last year"
    # gets resolved by the caller). Kept as a string to match other date-ish
    # nullable columns and avoid timezone math for a coarse value.
    gift_year: Mapped[str | None] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="gift_logs")
    contact: Mapped["Contact | None"] = relationship(back_populates="gift_logs")

    __table_args__ = (
        Index("ix_gift_log_user_contact", "user_id", "contact_id"),
    )


from app.models.user import User  # noqa: E402, F401
from app.models.contact import Contact  # noqa: E402, F401
