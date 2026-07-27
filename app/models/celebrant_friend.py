import enum
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CelebrantFriendStatus(str, enum.Enum):
    INVITED = "INVITED"      # Reminder sent, waiting for action
    VIEWED = "VIEWED"        # Friend opened the link/started conversation
    PURCHASED = "PURCHASED"  # Friend completed a gift purchase
    DECLINED = "DECLINED"    # Friend explicitly declined


class CelebrantFriend(Base):
    """A friend the celebrant wants reminded about their occasion.

    Stores phone for messaging, with optional link to User if they're registered.
    When an unregistered friend signs up, we can link user_id for tracking.
    """

    __tablename__ = "celebrant_friends"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    celebrant_occasion_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("celebrant_occasions.id", ondelete="CASCADE"),
        index=True,
    )

    # Phone number in E.164-ish format: 91XXXXXXXXXX (12 digits for India)
    phone: Mapped[str] = mapped_column(String(15), index=True)

    # Optional link to User table (populated when friend registers). Cross-
    # reference: if the friend deletes their User, the celebrant's invite
    # row stays (we still need to remember the phone was invited).
    user_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # Name as provided by celebrant (may differ from User.name if they register)
    name: Mapped[str | None] = mapped_column(String(100))

    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # When reminder was sent

    status: Mapped[CelebrantFriendStatus] = mapped_column(
        Enum(CelebrantFriendStatus), default=CelebrantFriendStatus.INVITED
    )

    celebrant_occasion: Mapped["CelebrantOccasion"] = relationship(back_populates="friends")
    user: Mapped["User | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("celebrant_occasion_id", "phone", name="uq_celebrant_friend_occasion_phone"),
    )


from app.models.celebrant_occasion import CelebrantOccasion  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
