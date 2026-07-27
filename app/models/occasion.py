import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Boolean, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OccasionType(str, enum.Enum):
    BIRTHDAY = "BIRTHDAY"
    ANNIVERSARY = "ANNIVERSARY"
    WEDDING = "WEDDING"
    DIWALI = "DIWALI"
    RAKSHA_BANDHAN = "RAKSHA_BANDHAN"
    VALENTINES = "VALENTINES"
    HOLI = "HOLI"
    EID = "EID"
    CHRISTMAS = "CHRISTMAS"
    NEW_YEAR = "NEW_YEAR"
    MOTHERS_DAY = "MOTHERS_DAY"
    FATHERS_DAY = "FATHERS_DAY"
    CUSTOM = "CUSTOM"


_DEFAULT_RECURRING_TYPES = {
    OccasionType.BIRTHDAY,
    OccasionType.ANNIVERSARY,
    OccasionType.DIWALI,
    OccasionType.RAKSHA_BANDHAN,
    OccasionType.VALENTINES,
    OccasionType.HOLI,
    OccasionType.EID,
    OccasionType.CHRISTMAS,
    OccasionType.NEW_YEAR,
    OccasionType.MOTHERS_DAY,
    OccasionType.FATHERS_DAY,
}


def default_recurring_for_occasion(occasion_type: OccasionType) -> bool:
    return occasion_type in _DEFAULT_RECURRING_TYPES


# Surface phrase used in the `voucher_delivered_friend` utility template body
# (param {{3}} — "...worth ₹500 for <phrase>."). Possessives only where the
# occasion belongs to a person; festivals stand alone.
_OCCASION_PHRASES: dict[OccasionType, str] = {
    OccasionType.BIRTHDAY: "your birthday",
    OccasionType.ANNIVERSARY: "your anniversary",
    OccasionType.WEDDING: "your wedding",
    OccasionType.DIWALI: "Diwali",
    OccasionType.RAKSHA_BANDHAN: "Raksha Bandhan",
    OccasionType.VALENTINES: "Valentine's Day",
    OccasionType.HOLI: "Holi",
    OccasionType.EID: "Eid",
    OccasionType.CHRISTMAS: "Christmas",
    OccasionType.NEW_YEAR: "the New Year",
    OccasionType.MOTHERS_DAY: "Mother's Day",
    OccasionType.FATHERS_DAY: "Father's Day",
}


def occasion_phrase(occasion_type: OccasionType, custom_label: str | None = None) -> str:
    """Render an OccasionType for inline use in a sentence."""
    if occasion_type == OccasionType.CUSTOM:
        return (custom_label.strip() if custom_label and custom_label.strip() else "your special day")
    return _OCCASION_PHRASES.get(occasion_type, "your special day")


class Occasion(Base):
    __tablename__ = "occasions"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Contact may be deleted independently (e.g. user removes a friend);
    # the occasion remains so historical purchases keep their context.
    contact_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    type: Mapped[OccasionType] = mapped_column(Enum(OccasionType))
    custom_label: Mapped[str | None] = mapped_column(String(100))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recurring: Mapped[bool] = mapped_column(Boolean, default=True)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    skipped_year: Mapped[str | None] = mapped_column(String(4))
    # Soft-delete: set when the owner deletes an occasion that still has
    # outstanding friend reminder-links. The occasion is hidden from all
    # owner-facing lists but the row (and its celebrant_occasion/friends)
    # survives so already-sent friend links keep resolving. See
    # get_celebrant_context and the FRIEND FLOW in the bot prompt.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="occasions")
    contact: Mapped["Contact | None"] = relationship(back_populates="occasions")
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="occasion",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    purchases: Mapped[list["Purchase"]] = relationship(back_populates="occasion")
    celebrant_occasion: Mapped["CelebrantOccasion | None"] = relationship(back_populates="occasion", uselist=False)


from app.models.user import User  # noqa: E402, F401
from app.models.contact import Contact  # noqa: E402, F401
from app.models.reminder import Reminder  # noqa: E402, F401
from app.models.purchase import Purchase  # noqa: E402, F401
from app.models.celebrant_occasion import CelebrantOccasion  # noqa: E402, F401
