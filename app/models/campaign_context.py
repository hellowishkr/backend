from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CampaignContext(Base):
    """Last marketing/acquisition template sent to a phone, for message-match.

    Written by ``send_template_and_record`` at send time. Consumed once by
    the inbound greeting short-circuit in ``agent.py`` (stamps
    ``consumed_at``) so a repeated "hi" doesn't keep replaying the same
    campaign-specific welcome. One row per phone — a new send overwrites
    the previous unconsumed context.
    """

    __tablename__ = "campaign_contexts"

    phone: Mapped[str] = mapped_column(String(15), primary_key=True)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    campaign_source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
