"""Private gift-planning state for one user and one occasion."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OccasionGiftPlan(Base):
    """Durable giver-owned context, separate from recipient-confirmed preferences."""

    __tablename__ = "occasion_gift_plans"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    occasion_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("occasions.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )

    budget_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")

    # These are the giver's private observations for this gift plan. They must
    # not be promoted into global RecipientPreference rows.
    interests: Mapped[list[str]] = mapped_column(JSON, default=list)
    vibe_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    desired_categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    excluded_categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_brands: Mapped[list[str]] = mapped_column(JSON, default=list)
    disliked_brands: Mapped[list[str]] = mapped_column(JSON, default=list)
    gift_format: Mapped[str | None] = mapped_column(String(30), nullable=True)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    recipient_city: Mapped[str | None] = mapped_column(String(80), nullable=True)

    selected_candidate_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    selected_provider_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    selected_brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_suggestions: Mapped[list[dict]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "occasion_id",
            name="uq_occasion_gift_plan_user_occasion",
        ),
    )
