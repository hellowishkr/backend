import enum
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ReminderStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    occasion_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("occasions.id", ondelete="CASCADE"), index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReminderStatus] = mapped_column(Enum(ReminderStatus), default=ReminderStatus.PENDING)
    message_type: Mapped[str | None] = mapped_column(String(50))  # gift_card_reminder, day_of_reminder, tight_window_reminder, ad_hoc_reminder …
    # Meta Cloud API message id (wamid) returned on send. We moved off Interakt;
    # the underlying column was renamed by migration 2a8f4e19c7b3. Populated
    # by dispatch_reminder() for delivery-status correlation. NULL for rows
    # that haven't been dispatched yet (PENDING) or pre-migration history.
    whatsapp_msg_id: Mapped[str | None] = mapped_column(String(100))
    target_phone: Mapped[str | None] = mapped_column(String(15))  # if set, send to this phone instead of occasion owner
    # Retry bookkeeping. A transient WhatsApp/Meta blip used to flip the row
    # to FAILED forever; with these we let `send_due_reminders` reschedule
    # PENDING with a backoff up to ``_MAX_REMINDER_ATTEMPTS``.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))

    occasion: Mapped["Occasion"] = relationship(back_populates="reminders")

    __table_args__ = (
        Index("ix_reminders_status_scheduled", "status", "scheduled_at"),
    )


from app.models.occasion import Occasion  # noqa: E402, F401
