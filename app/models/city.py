from sqlalchemy import String, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class City(Base):
    __tablename__ = "cities"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # slug: "pune", "delhi-ncr"
    name: Mapped[str] = mapped_column(String(60))
    state: Mapped[str | None] = mapped_column(String(60))
    country: Mapped[str | None] = mapped_column(String(60), nullable=True)
    aliases: Mapped[list[str] | None] = mapped_column(JSON)  # ["Bombay", "Mumbai"]
    # Pincode prefixes this city covers, e.g. ["4110", "4111", "4130"] for Pune.
    # A destination pincode matches the city if it starts with any prefix.
    pincode_prefixes: Mapped[list[str] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="verified")  # verified | pending
    auto_created: Mapped[bool] = mapped_column(Boolean, default=False)
