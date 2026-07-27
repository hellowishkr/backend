import json
from datetime import datetime

from sqlalchemy import String, DateTime, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CelebrantOccasion(Base):
    """Extension table for celebrant-initiated occasions (1:1 with Occasion).

    Stores gift preferences and wishlist URLs for the celebrant's occasion.
    Friends responding to reminders will see these preferences.
    """

    __tablename__ = "celebrant_occasions"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    occasion_id: Mapped[str] = mapped_column(
        String(30),
        ForeignKey("occasions.id", ondelete="CASCADE"),
        unique=True, index=True,
    )

    # Gift preferences - JSON array of categories like ["Gift Cards", "Flowers", "Experiences"]
    preferences: Mapped[str] = mapped_column(Text, default="[]")

    # Wishlist URLs - JSON array of {url, label} objects for any platform
    # e.g. [{"url": "https://amazon.in/wishlist/xyz", "label": "Amazon Wishlist"}]
    wishlist_urls: Mapped[str | None] = mapped_column(Text)

    # Optional note from celebrant to friends
    note_to_friends: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    occasion: Mapped["Occasion"] = relationship(back_populates="celebrant_occasion")
    friends: Mapped[list["CelebrantFriend"]] = relationship(back_populates="celebrant_occasion")

    @property
    def preferences_list(self) -> list[str]:
        """Get gift preference categories as a list."""
        try:
            return json.loads(self.preferences) if self.preferences else []
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def wishlists(self) -> list[dict]:
        """Get wishlist URLs as a list of {url, label} dicts."""
        try:
            return json.loads(self.wishlist_urls) if self.wishlist_urls else []
        except (json.JSONDecodeError, TypeError):
            return []


from app.models.occasion import Occasion  # noqa: E402, F401
from app.models.celebrant_friend import CelebrantFriend  # noqa: E402, F401
