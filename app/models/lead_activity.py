from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LeadActivity(Base):
    """Timeline entry for a vendor or corporate lead.

    Not FK-constrained to vendor_leads/corporate_leads — ``lead_kind`` +
    ``lead_id`` is a polymorphic reference, mirroring how admin_leads.py
    already treats the two pipelines as siblings rather than sharing a table.
    """

    __tablename__ = "lead_activities"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    lead_kind: Mapped[str] = mapped_column(String(20), index=True)  # vendor | corporate
    lead_id: Mapped[str] = mapped_column(String(30), index=True)
    type: Mapped[str] = mapped_column(String(20))  # note | call | status_change | follow_up
    body: Mapped[str | None] = mapped_column(Text)
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
