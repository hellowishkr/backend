from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    # owner | ops | vendor_success
    role: Mapped[str] = mapped_column(String(30), default="ops")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    body: Mapped[str] = mapped_column(String(2000))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PricingSetting(Base):
    """Single-row store for the storefront markup + flash-discount rates.

    While active, customer pays base_price × (1 + markup_pct/100) ×
    (1 - flash_off_pct/100). The 90/10 vendor split still runs on the product's
    normal discounted price. Web storefront only.
    """

    __tablename__ = "pricing_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    markup_pct: Mapped[Numeric] = mapped_column(Numeric(5, 2), default=30)
    flash_off_pct: Mapped[Numeric] = mapped_column(Numeric(5, 2), default=20)
    flash_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    actor: Mapped[str | None] = mapped_column(String(255))  # admin email or "system"
    action: Mapped[str] = mapped_column(String(80))          # e.g. "vendor.update"
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(40))
    summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
