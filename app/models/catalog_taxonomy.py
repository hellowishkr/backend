"""Storefront product taxonomy — categories and gifting occasions.

Single source of truth for the lists shown in the admin product editor, the
vendor self-service editor, and the storefront shop filters. Previously these
were hardcoded constants duplicated across wishkr-admin and wishkr-web; now
all three read GET /api/catalog/taxonomy (Redis-cached).

Products reference these by value: Product.category stores the category
display name (exact match, e.g. "Flowers"), Product.occasions stores a list
of occasion slugs (e.g. "birthday").
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProductCategory(Base):
    __tablename__ = "product_categories"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # slug: "flowers"
    name: Mapped[str] = mapped_column(String(60))  # display label: "Flowers"
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class GiftingOccasion(Base):
    __tablename__ = "gifting_occasions"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # slug: "birthday"
    name: Mapped[str] = mapped_column(String(60))  # display label: "Birthday"
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
