from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(15))
    relationship_type: Mapped[str | None] = mapped_column(String(30))  # friend, spouse, parent, sibling, colleague
    city_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("cities.id", ondelete="SET NULL"), nullable=True
    )
    city_raw: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="contacts")
    occasions: Mapped[list["Occasion"]] = relationship(back_populates="contact")
    gift_logs: Mapped[list["GiftLog"]] = relationship(back_populates="contact")

    __table_args__ = (
        UniqueConstraint("user_id", "phone", name="uq_user_contact_phone"),
    )


from app.models.user import User  # noqa: E402, F401
from app.models.occasion import Occasion  # noqa: E402, F401
from app.models.gift_log import GiftLog  # noqa: E402, F401
