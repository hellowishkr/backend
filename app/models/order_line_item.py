from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import Order


class OrderLineItem(Base):
    __tablename__ = "order_line_items"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("product_variants.id", ondelete="SET NULL")
    )
    # Snapshot of the product's name/image at order time, same rationale as
    # selected_addons below: order confirmations/history must show what the
    # buyer actually saw and bought, even if the vendor later renames the
    # product or swaps its image (product_id itself uses ondelete=RESTRICT,
    # so a product with any order history can't be deleted at all).
    product_name: Mapped[str] = mapped_column(String(160))
    product_image_url: Mapped[str | None] = mapped_column(String(500))
    # Snapshot of the add-ons chosen at order time. Each entry is a dict:
    #   {addon_id, name, unit_price_paise, quantity, total_price_paise, note?}
    # Stored as a frozen snapshot so order history shows the exact name/price/qty
    # even if the ProductAddon is later edited or deleted.
    selected_addons: Mapped[list[dict]] = mapped_column(JSON, default=list)
    customizations: Mapped[dict] = mapped_column(JSON, default=dict)
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_paise: Mapped[int] = mapped_column(Integer)
    line_total_paise: Mapped[int] = mapped_column(Integer)
    shipping_fee_paise: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    order: Mapped["Order"] = relationship(back_populates="line_items")
