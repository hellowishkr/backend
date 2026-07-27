import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Text, Enum, Index, UniqueConstraint, func, BigInteger
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class WebhookProvider(str, enum.Enum):
    RAZORPAY = "RAZORPAY"
    WHATSAPP = "WHATSAPP"
    HUBBLE = "HUBBLE"


class WebhookEventStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


class WebhookEvent(Base):
    """Immutable audit log of every provider webhook we received.

    DEFERRED (#26): ``payload`` JSONB stores full Razorpay/WhatsApp payloads
    which include payer name + contact + sometimes card last-4. Treat the
    backups as PII-grade. When DPDP enforcement kicks in, encrypt the
    column with pgcrypto or move payloads to an encrypted-at-rest object
    store and keep only the ids here.
    """

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[WebhookProvider] = mapped_column(Enum(WebhookProvider), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Idempotency key from the provider. UNIQUE per (provider, event_id) so a
    # racing redelivery is rejected by the DB rather than a non-atomic SELECT.
    event_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature: Mapped[str | None] = mapped_column(String(255))
    signature_valid: Mapped[bool | None] = mapped_column()
    status: Mapped[WebhookEventStatus] = mapped_column(
        Enum(WebhookEventStatus), default=WebhookEventStatus.RECEIVED, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(String(30), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_webhook_events_provider_type", "provider", "event_type"),
        Index("ix_webhook_events_received_at", "received_at"),
        # NULL event_ids (events without an upstream id) are not deduped —
        # Postgres treats NULL != NULL in unique indexes, which is correct here.
        UniqueConstraint("provider", "event_id", name="uq_webhook_events_provider_event_id"),
    )
