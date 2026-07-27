from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VendorCategory(Base):
    __tablename__ = "vendor_categories"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # slug: "cake", "jewellery"
    name: Mapped[str] = mapped_column(String(60))  # display label: "Cake", "Jewellery"
    auto_created: Mapped[bool] = mapped_column(Boolean, default=False)
