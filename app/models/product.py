from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.vendor import Vendor


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "discounted_price_paise IS NULL OR discounted_price_paise <= base_price_paise",
            name="ck_products_discount_not_above_base",
        ),
        CheckConstraint(
            "inventory_quantity IS NULL OR inventory_quantity >= 0",
            name="ck_products_inventory_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(60), index=True)
    occasions: Mapped[list[str]] = mapped_column(JSON, default=list)
    preparation_time: Mapped[str | None] = mapped_column(String(120))
    dispatch_lead_time_days: Mapped[int | None] = mapped_column(Integer)
    delivery_pincodes: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_bestseller: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_customisable: Mapped[bool] = mapped_column(Boolean, default=False)
    # True = this vendor accepts a gift message for this product; the storefront
    # only shows the gift-message field when this is set.
    supports_gift_message: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # True = ships pan-India; False = local-only, delivers only where the
    # vendor operates (vendor.city_ids). See app/utils/serviceability.py.
    ships_nationally: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    product_images: Mapped[list[str]] = mapped_column(JSON, default=list)
    care_instructions: Mapped[str | None] = mapped_column(Text)
    whats_included: Mapped[str | None] = mapped_column(Text)
    customization_schema: Mapped[list[dict]] = mapped_column(JSON, default=list)
    # Vendor's original/reference price. In normal mode this is the crossed-out
    # price; in flash mode it is the input to the global markup/discount.
    base_price_paise: Mapped[int] = mapped_column(Integer)
    # Vendor's normal customer price. Null means no vendor discount, so base is
    # charged. Global flash temporarily replaces this price while active.
    discounted_price_paise: Mapped[int | None] = mapped_column(Integer)
    # Null means inventory is intentionally unlimited/untracked. Zero means sold
    # out. `in_stock` remains the vendor's instant manual availability switch.
    inventory_quantity: Mapped[int | None] = mapped_column(Integer)
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    hsn: Mapped[str | None] = mapped_column(String(20))
    shipping_included: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    shipping_fee_paise: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(500))
    video_url: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Vendor-controlled availability, toggled instantly (no admin review): when
    # False the product still shows on the storefront but can't be bought. Kept
    # separate from `active`/`lifecycle_status`, which are admin publish controls.
    in_stock: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # Richer lifecycle: draft | active | paused | archived. `active` (bool) stays
    # the storefront source of truth and is kept in sync (active == "active").
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    vendor: Mapped["Vendor"] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    addons: Mapped[list["ProductAddon"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_product_variants_product_name"),
        CheckConstraint(
            "discounted_price_paise IS NULL OR discounted_price_paise <= price_delta_paise",
            name="ck_product_variants_discount_not_above_base",
        ),
        CheckConstraint(
            "inventory_quantity IS NULL OR inventory_quantity >= 0",
            name="ck_product_variants_inventory_nonnegative",
        ),
        CheckConstraint(
            "seasonal_price_paise IS NULL OR seasonal_price_paise >= 0",
            name="ck_product_variants_seasonal_price_nonnegative",
        ),
        CheckConstraint(
            "seasonal_price_start IS NULL OR seasonal_price_end IS NULL "
            "OR seasonal_price_start <= seasonal_price_end",
            name="ck_product_variants_seasonal_window",
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    # Legacy name: for a selected variant this is its absolute original/base
    # price, replacing Product.base_price_paise.
    price_delta_paise: Mapped[int] = mapped_column(Integer, default=0)
    discounted_price_paise: Mapped[int | None] = mapped_column(Integer)
    inventory_quantity: Mapped[int | None] = mapped_column(Integer)
    # Optional time-bounded selling-price override. When active it replaces the
    # normal variant base/discounted price in both catalogue display and
    # backend order pricing. Null dates make the override open-ended.
    seasonal_price_paise: Mapped[int | None] = mapped_column(Integer)
    seasonal_price_start: Mapped[date | None] = mapped_column(Date)
    seasonal_price_end: Mapped[date | None] = mapped_column(Date)
    # Admin-only costing recipe. Public catalogue serialization emits a
    # sanitized composition (names + quantities) but never procurement,
    # making, packaging, or margin inputs.
    bouquet_recipe: Mapped[dict | None] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    product: Mapped["Product"] = relationship(back_populates="variants")


class ProductAddon(Base):
    __tablename__ = "product_addons"
    __table_args__ = (UniqueConstraint("product_id", "name", name="uq_product_addons_product_name"),)

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    price_paise: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Quantity configuration. When allow_quantity is False the buyer may only
    # select the add-on once (quantity is forced to 1). When True they pick a
    # quantity between min_quantity and max_quantity (e.g. Teddy Bear x 3).
    allow_quantity: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    min_quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    max_quantity: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    product: Mapped["Product"] = relationship(back_populates="addons")
