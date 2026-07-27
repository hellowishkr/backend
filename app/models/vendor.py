from datetime import datetime

from sqlalchemy import Float, Integer, String, Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    # `name` is the brand / display name shown everywhere across the storefront,
    # admin, and notifications. `company_name`/`company_address` are the legal
    # registered entity details (often the same as the brand, hence the
    # "same as brand name" convenience in the admin editor).
    name: Mapped[str] = mapped_column(String(120))
    company_name: Mapped[str | None] = mapped_column(String(160))
    company_address: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60), index=True)  # private_theatre, cake, jewellery, gift_card...
    # normal | corporate | both — which buying flow this vendor's products are eligible for.
    vendor_type: Mapped[str] = mapped_column(String(20), default="normal", server_default="normal", index=True)
    razorpay_account_id: Mapped[str | None] = mapped_column(String(80))
    phone: Mapped[str | None] = mapped_column(String(20))
    gstin: Mapped[str | None] = mapped_column(String(20))
    pan: Mapped[str | None] = mapped_column(String(20))
    bank_account_name: Mapped[str | None] = mapped_column(String(120))
    bank_account_number: Mapped[str | None] = mapped_column(String(40))
    bank_ifsc: Mapped[str | None] = mapped_column(String(20))
    contact_info: Mapped[str | None] = mapped_column(Text)
    poc_name: Mapped[str | None] = mapped_column(String(120))
    poc_email: Mapped[str | None] = mapped_column(String(160))
    fulfillment_mode: Mapped[str] = mapped_column(String(20), default="manual")
    # Who handles shipping after an order is placed: self (vendor's own courier)
    # vs wishkr (Wishkr arranges pickup/delivery).
    delivery_arrangement: Mapped[str] = mapped_column(
        String(20), default="self", server_default="self"
    )
    delivery_policy: Mapped[str | None] = mapped_column(Text)
    return_policy: Mapped[str | None] = mapped_column(Text)
    cancellation_policy: Mapped[str | None] = mapped_column(Text)
    delivery_lead_time_hours: Mapped[int | None] = mapped_column(Integer)
    same_day_cutoff_hour: Mapped[int | None] = mapped_column(Integer)
    # When set, this is the complete shipping charge for the vendor's order,
    # regardless of product mix or quantity. ``None`` keeps the legacy product
    # base fee + per-extra-unit calculation below.
    shipping_flat_fee_paise: Mapped[int | None] = mapped_column(Integer)
    # Legacy fallback for vendors without a flat order fee: added per extra unit
    # after the highest product shipping fee has been charged for the first unit.
    shipping_extra_item_fee_paise: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    min_price: Mapped[int | None] = mapped_column(Integer)
    max_price: Mapped[int | None] = mapped_column(Integer)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.5)
    is_national: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Onboarding pipeline: prospect | contacted | kyc_pending | kyc_done | live | paused
    onboarding_status: Mapped[str] = mapped_column(
        String(20), default="live", server_default="live", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    cities: Mapped[list["VendorCity"]] = relationship(back_populates="vendor", cascade="all, delete-orphan")
    products: Mapped[list["Product"]] = relationship(back_populates="vendor", cascade="all, delete-orphan")
    documents: Mapped[list["VendorDocument"]] = relationship(back_populates="vendor", cascade="all, delete-orphan")
    category_links: Mapped[list["VendorCategoryLink"]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan"
    )


class VendorCity(Base):
    __tablename__ = "vendor_cities"

    vendor_id: Mapped[str] = mapped_column(String(30), ForeignKey("vendors.id", ondelete="CASCADE"), primary_key=True)
    city_id: Mapped[str] = mapped_column(String(40), ForeignKey("cities.id", ondelete="CASCADE"), primary_key=True)

    vendor: Mapped["Vendor"] = relationship(back_populates="cities")


class VendorCategoryLink(Base):
    __tablename__ = "vendor_category_links"

    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[str] = mapped_column(
        String(60), ForeignKey("vendor_categories.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    vendor: Mapped["Vendor"] = relationship(back_populates="category_links")
