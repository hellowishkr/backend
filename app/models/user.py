from datetime import datetime

from sqlalchemy import String, DateTime, Boolean, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    phone: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    city_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("cities.id", ondelete="SET NULL"), nullable=True
    )
    city_raw: Mapped[str | None] = mapped_column(String(60), nullable=True)
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Asia/Kolkata",
        server_default="Asia/Kolkata",
        nullable=False,
    )
    is_guest: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # Stamp of the user's most recent inbound WhatsApp message. Used to decide
    # whether the 24h Meta customer-service window is open (free-text send)
    # or closed (must use an approved utility template).
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    contacts: Mapped[list["Contact"]] = relationship(back_populates="user")
    occasions: Mapped[list["Occasion"]] = relationship(back_populates="user")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    gift_logs: Mapped[list["GiftLog"]] = relationship(back_populates="user")


# Avoid circular imports — these are resolved at runtime by SQLAlchemy
from app.models.contact import Contact  # noqa: E402, F401
from app.models.occasion import Occasion  # noqa: E402, F401
from app.models.order import Order  # noqa: E402, F401
from app.models.gift_log import GiftLog  # noqa: E402, F401
