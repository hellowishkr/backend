from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FailedOutboundMessage(Base):
    """Best-effort durable record when WhatsApp send fails after retries.

    ``template_name`` / ``template_params_json`` are populated when the
    original send used an approved utility template (voucher_delivered,
    voucher_delivered_friend). The retry job then replays as a template —
    a free-text replay outside the 24h CS window would be rejected by
    Meta and burn through the retry budget without ever delivering.
    ``message_body`` stays populated as a human-readable fallback (and so
    older retry rows without template metadata still replay correctly).
    """

    __tablename__ = "failed_outbound_messages"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    phone: Mapped[str] = mapped_column(String(15), index=True, nullable=False)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    send_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Template-replay metadata. Both NULL → retry as send_text. Both set →
    # retry as send_template with the persisted params.
    template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    template_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
