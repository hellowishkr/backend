import hmac
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.catalog import mint_preview_token, serialize_product
from app.core.config import settings
from app.db.session import get_db
from app.models.city import City
from app.models.order_line_item import OrderLineItem
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor, VendorCategoryLink, VendorCity
from app.models.vendor_category import VendorCategory
from app.models.vendor_document import DOC_TYPES, REQUIRED_DOC_TYPES, VendorDocument
from app.utils.cities import invalidate_verified_cache
from app.utils.html_sanitize import sanitize_html
from app.utils.id_gen import generate_id


router = APIRouter(prefix="/admin/catalog", tags=["admin-catalog"])


def require_admin_token(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = settings.admin_api_token
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="forbidden")


def require_role(*roles: str):
    """Enforce the caller's role against ``roles``.

    The admin web app forwards the logged-in user's role via the ``X-Admin-Role``
    header (set by its server-side proxy from the signed session cookie). The
    shared ``X-Admin-Token`` still gates all admin routes server-to-server.

    The role header must be present and explicit — holding the shared token only
    proves you're an authorized backend caller, it must not also imply full
    admin privileges. A missing/blank role header is denied, never defaulted to
    ``owner``.
    """

    allowed = set(roles)

    def _dep(
        _: None = Depends(require_admin_token),
        x_admin_role: Annotated[str | None, Header()] = None,
    ) -> None:
        role = (x_admin_role or "").strip().lower()
        if role == "owner":
            return None  # owner can do everything
        if not role or role not in allowed:
            raise HTTPException(status_code=403, detail="insufficient role")
        return None

    return _dep


class VendorPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)  # brand / display name
    company_name: str | None = Field(default=None, max_length=160)
    company_address: str | None = None
    # Multi-category. `category` (single, primary) is kept in sync with
    # category_ids[0] for backward compat with existing filters/queries.
    category_ids: list[str] = Field(default_factory=list)
    category: str | None = Field(default=None, max_length=60)
    vendor_type: str = "normal"  # normal | corporate | both
    contact_info: str | None = None
    poc_name: str | None = None
    poc_email: str | None = None
    phone: str | None = None
    razorpay_account_id: str | None = None
    gstin: str | None = None
    pan: str | None = None
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    fulfillment_mode: str = "manual"
    delivery_arrangement: str = "self"  # self | wishkr
    shipping_flat_fee_paise: int | None = Field(default=None, ge=0)
    shipping_extra_item_fee_paise: int = Field(default=0, ge=0)
    delivery_policy: str | None = None
    return_policy: str | None = None
    cancellation_policy: str | None = None
    delivery_lead_time_hours: int | None = None
    same_day_cutoff_hour: int | None = None
    min_price: int | None = None
    max_price: int | None = None
    is_national: bool = False
    reliability_score: float = 0.5
    active: bool = True
    onboarding_status: str = "live"
    city_ids: list[str] = Field(default_factory=list)


class BouquetComponentPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["flower", "filler", "chocolate", "gift", "packaging", "other"] = (
        "flower"
    )
    quantity: int = Field(ge=1)
    unit_label: str = Field(default="stem", min_length=1, max_length=30)
    lot_size: int = Field(default=1, ge=1)
    off_season_lot_cost_paise: int = Field(default=0, ge=0)
    peak_season_lot_cost_paise: int | None = Field(default=None, ge=0)


class BouquetRecipePayload(BaseModel):
    enabled: bool = True
    components: list[BouquetComponentPayload] = Field(default_factory=list)
    making_cost_paise: int = Field(default=0, ge=0)
    packaging_cost_paise: int = Field(default=0, ge=0)
    other_cost_paise: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _enabled_recipe_has_components(self) -> "BouquetRecipePayload":
        if self.enabled and not self.components:
            raise ValueError("an enabled bouquet recipe must have at least one component")
        return self


class VariantPayload(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    sku: str | None = None
    price_delta_paise: int = 0
    discounted_price_paise: int | None = Field(default=None, ge=0)
    inventory_quantity: int | None = Field(default=None, ge=0)
    seasonal_price_paise: int | None = Field(default=None, ge=0)
    seasonal_price_start: date | None = None
    seasonal_price_end: date | None = None
    bouquet_recipe: BouquetRecipePayload | None = None
    active: bool = True
    sort_order: int = 0

    # SKU is unique table-wide and nullable. A blank string is a real value, so
    # two variants without a SKU collide on product_variants_sku_key; store None.
    @field_validator("sku", mode="before")
    @classmethod
    def _blank_sku_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _check_discounted_price(self) -> "VariantPayload":
        if (
            self.discounted_price_paise is not None
            and self.discounted_price_paise > self.price_delta_paise
        ):
            raise ValueError(
                "variant discounted price must be less than or equal to base price"
            )
        if (
            self.seasonal_price_start is not None
            and self.seasonal_price_end is not None
            and self.seasonal_price_start > self.seasonal_price_end
        ):
            raise ValueError("seasonal price start must not be after its end")
        if (
            self.seasonal_price_paise is None
            and (
                self.seasonal_price_start is not None
                or self.seasonal_price_end is not None
            )
        ):
            raise ValueError("seasonal dates require a seasonal price")
        return self


# Postgres int4 ceiling — the max value the max_quantity column (Integer) can
# hold. There's no product-level cap on per-add-on quantity; the only limit is
# what the column can store, so bulk/corporate add-ons aren't artificially
# blocked. min <= max and min >= 1 are still enforced below.
MAX_ADDON_QUANTITY = 2_147_483_647


class AddonPayload(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    price_paise: int = 0
    active: bool = True
    sort_order: int = 0
    allow_quantity: bool = False
    min_quantity: int = 1
    max_quantity: int = 1

    @model_validator(mode="after")
    def _check_quantity(self) -> "AddonPayload":
        if not self.allow_quantity:
            # Single-select add-on: quantity is always exactly 1.
            self.min_quantity = 1
            self.max_quantity = 1
            return self
        if self.min_quantity < 1:
            raise ValueError("min_quantity must be at least 1")
        if self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity must be greater than or equal to min_quantity")
        if self.max_quantity > MAX_ADDON_QUANTITY:
            raise ValueError(f"max_quantity must not exceed {MAX_ADDON_QUANTITY}")
        return self


class ProductPayload(BaseModel):
    vendor_id: str
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    category: str | None = Field(default=None, max_length=60)
    occasions: list[str] = Field(default_factory=list)
    preparation_time: str | None = Field(default=None, max_length=120)
    dispatch_lead_time_days: int | None = None
    delivery_pincodes: list[str] = Field(default_factory=list)
    ships_nationally: bool = True
    is_bestseller: bool = False
    is_customisable: bool = False
    supports_gift_message: bool = False
    product_images: list[str] = Field(default_factory=list)
    care_instructions: str | None = None
    whats_included: str | None = None
    customization_schema: list[dict] = Field(default_factory=list)
    base_price_paise: int = Field(ge=0)
    discounted_price_paise: int | None = Field(default=None, ge=0)
    inventory_quantity: int | None = Field(default=None, ge=0)
    gst_rate: Decimal | None = None
    hsn: str | None = None
    shipping_included: bool = True
    shipping_fee_paise: int | None = None
    image_url: str | None = None
    video_url: str | None = None
    active: bool = True
    in_stock: bool = True
    lifecycle_status: str | None = None  # draft | active | paused | archived
    variants: list[VariantPayload] = Field(default_factory=list)
    addons: list[AddonPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_discounted_price(self) -> "ProductPayload":
        if (
            self.discounted_price_paise is not None
            and self.discounted_price_paise > self.base_price_paise
        ):
            raise ValueError(
                "product discounted price must be less than or equal to base price"
            )
        return self


_FINANCIAL_ROLES = {"owner", "ops"}


def _mask_account_number(value: str | None) -> str | None:
    if not value:
        return value
    return f"****{value[-4:]}" if len(value) > 4 else "****"


def serialize_vendor(
    vendor: Vendor, *, role: str = "owner", product_count: int | None = None
) -> dict:
    can_view_financials = role.strip().lower() in _FINANCIAL_ROLES
    return {
        "id": vendor.id,
        "name": vendor.name,
        "company_name": vendor.company_name,
        "company_address": vendor.company_address,
        "category": vendor.category,
        "category_ids": [link.category_id for link in vendor.category_links],
        "vendor_type": getattr(vendor, "vendor_type", "normal"),
        "contact_info": vendor.contact_info,
        "poc_name": vendor.poc_name,
        "poc_email": vendor.poc_email,
        "phone": vendor.phone,
        "razorpay_account_id": vendor.razorpay_account_id,
        "gstin": vendor.gstin if can_view_financials else None,
        "pan": vendor.pan if can_view_financials else None,
        "bank_account_name": vendor.bank_account_name if can_view_financials else None,
        "bank_account_number": vendor.bank_account_number
        if can_view_financials
        else _mask_account_number(vendor.bank_account_number),
        "bank_ifsc": vendor.bank_ifsc if can_view_financials else None,
        "fulfillment_mode": vendor.fulfillment_mode,
        "delivery_arrangement": getattr(vendor, "delivery_arrangement", "self"),
        "shipping_flat_fee_paise": getattr(vendor, "shipping_flat_fee_paise", None),
        "shipping_extra_item_fee_paise": getattr(vendor, "shipping_extra_item_fee_paise", 0),
        "delivery_policy": vendor.delivery_policy,
        "return_policy": vendor.return_policy,
        "cancellation_policy": vendor.cancellation_policy,
        "delivery_lead_time_hours": vendor.delivery_lead_time_hours,
        "same_day_cutoff_hour": vendor.same_day_cutoff_hour,
        "min_price": vendor.min_price,
        "max_price": vendor.max_price,
        "is_national": vendor.is_national,
        "reliability_score": vendor.reliability_score,
        "active": vendor.active,
        "onboarding_status": getattr(vendor, "onboarding_status", "live"),
        "city_ids": [city.city_id for city in vendor.cities],
        "product_count": product_count,
    }


def apply_vendor_payload(vendor: Vendor, payload: VendorPayload) -> None:
    for key, value in payload.model_dump(exclude={"city_ids", "category_ids", "category"}).items():
        setattr(vendor, key, value)
    # The order policy no longer charges per-extra-item delivery.
    vendor.shipping_extra_item_fee_paise = 0
    # Primary category stays the source of truth for legacy filters: first of
    # the selected categories, falling back to an explicit `category` if given.
    primary = (payload.category_ids[0] if payload.category_ids else None) or payload.category
    if primary:
        vendor.category = primary


async def replace_vendor_categories(
    db: AsyncSession, vendor: Vendor, category_ids: list[str]
) -> None:
    await db.execute(
        delete(VendorCategoryLink).where(VendorCategoryLink.vendor_id == vendor.id)
    )
    # Dedupe, preserve order. Skip ids that don't exist in vendor_categories.
    seen: set[str] = set()
    for cid in category_ids:
        if cid in seen:
            continue
        seen.add(cid)
        if await db.get(VendorCategory, cid) is not None:
            db.add(VendorCategoryLink(vendor_id=vendor.id, category_id=cid))


async def replace_vendor_cities(db: AsyncSession, vendor: Vendor, city_ids: list[str]) -> None:
    await db.execute(delete(VendorCity).where(VendorCity.vendor_id == vendor.id))
    for city_id in city_ids:
        db.add(VendorCity(vendor_id=vendor.id, city_id=city_id))


async def get_product_or_404(db: AsyncSession, product_id: str) -> Product:
    product = (
        await db.execute(
            select(Product)
            .where(Product.id == product_id)
            .options(
                selectinload(Product.vendor),
                selectinload(Product.variants),
                selectinload(Product.addons),
            )
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


async def sync_product_children(
    db: AsyncSession,
    product: Product,
    variants: list[VariantPayload],
    addons: list[AddonPayload],
    sync_variants: bool = True,
    sync_addons: bool = True,
) -> None:
    """Reconcile ``product.variants``/``product.addons`` against the submitted
    lists. ``sync_variants``/``sync_addons`` let a caller that only received one
    collection (e.g. an addon-only edit) reconcile it without treating the
    other, omitted collection as "delete everything"."""
    keep_variant_ids: set[str] = set()
    for index, payload in enumerate(variants):
        variant = None
        if payload.id:
            variant = next((item for item in product.variants if item.id == payload.id), None)
        if variant is None:
            # No id (or id didn't match) — fall back to matching the existing
            # variant by name so an edit updates it in place instead of
            # colliding with it on the (product_id, name) unique constraint.
            variant = next(
                (item for item in product.variants if item.id not in keep_variant_ids and item.name == payload.name),
                None,
            )
        if variant is None:
            variant = ProductVariant(id=payload.id or generate_id("var"), product_id=product.id)
            db.add(variant)
        keep_variant_ids.add(variant.id)
        variant.name = payload.name
        variant.sku = payload.sku
        variant.price_delta_paise = payload.price_delta_paise
        # Preserve values when an older admin/vendor client submits a variant
        # payload that predates these fields.
        if "discounted_price_paise" in payload.model_fields_set:
            variant.discounted_price_paise = payload.discounted_price_paise
        if "inventory_quantity" in payload.model_fields_set:
            variant.inventory_quantity = payload.inventory_quantity
        if "seasonal_price_paise" in payload.model_fields_set:
            variant.seasonal_price_paise = payload.seasonal_price_paise
        if "seasonal_price_start" in payload.model_fields_set:
            variant.seasonal_price_start = payload.seasonal_price_start
        if "seasonal_price_end" in payload.model_fields_set:
            variant.seasonal_price_end = payload.seasonal_price_end
        if "bouquet_recipe" in payload.model_fields_set:
            variant.bouquet_recipe = (
                payload.bouquet_recipe.model_dump()
                if payload.bouquet_recipe is not None
                else None
            )
        variant.active = payload.active
        variant.sort_order = payload.sort_order or index + 1

    if sync_variants:
        for variant in list(product.variants):
            if variant.id not in keep_variant_ids:
                await db.delete(variant)

    keep_addon_ids: set[str] = set()
    for index, payload in enumerate(addons):
        addon = None
        if payload.id:
            addon = next((item for item in product.addons if item.id == payload.id), None)
        if addon is None:
            # No id (or id didn't match) — fall back to matching the existing
            # add-on by name so an edit updates it in place instead of
            # colliding with it on the (product_id, name) unique constraint.
            addon = next(
                (item for item in product.addons if item.id not in keep_addon_ids and item.name == payload.name),
                None,
            )
        if addon is None:
            addon = ProductAddon(id=payload.id or generate_id("add"), product_id=product.id)
            db.add(addon)
        keep_addon_ids.add(addon.id)
        addon.name = payload.name
        addon.price_paise = payload.price_paise
        addon.active = payload.active
        addon.sort_order = payload.sort_order or index + 1
        addon.allow_quantity = payload.allow_quantity
        addon.min_quantity = payload.min_quantity
        addon.max_quantity = payload.max_quantity

    if not sync_addons:
        return

    for addon in list(product.addons):
        if addon.id not in keep_addon_ids:
            await db.delete(addon)


def _slugify_category(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "uncategorised"


class VendorCategoryPayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get(
    "/vendor-categories",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def list_vendor_categories(db: AsyncSession = Depends(get_db)) -> dict:
    categories = (
        await db.execute(select(VendorCategory).order_by(VendorCategory.name))
    ).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(VendorCategoryLink.category_id, func.count())
                .group_by(VendorCategoryLink.category_id)
            )
        ).all()
    )
    return {
        "categories": [
            {
                "id": c.id,
                "name": c.name,
                "auto_created": c.auto_created,
                "vendor_count": counts.get(c.id, 0),
            }
            for c in categories
        ]
    }


def serialize_city(city: City) -> dict:
    return {
        "id": city.id,
        "name": city.name,
        "state": city.state,
        "aliases": city.aliases or [],
        "pincode_prefixes": city.pincode_prefixes or [],
        "status": city.status,
        "auto_created": city.auto_created,
    }


class CityPayload(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    state: str | None = Field(default=None, max_length=60)
    aliases: list[str] = Field(default_factory=list)
    pincode_prefixes: list[str] = Field(default_factory=list)
    status: str = "verified"  # verified | pending


@router.get(
    "/cities",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def list_cities(
    db: AsyncSession = Depends(get_db),
    include_pending: bool = Query(default=False),
) -> dict:
    stmt = select(City).order_by(City.name)
    if not include_pending:
        stmt = stmt.where(City.status == "verified")
    cities = (await db.execute(stmt)).scalars().all()
    return {"cities": [serialize_city(c) for c in cities]}


@router.post(
    "/cities",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def create_city(payload: CityPayload, db: AsyncSession = Depends(get_db)) -> dict:
    slug = _slugify_category(payload.name)
    if await db.get(City, slug) is not None:
        raise HTTPException(status_code=409, detail="city already exists")
    city = City(
        id=slug,
        name=payload.name.strip(),
        state=payload.state,
        aliases=payload.aliases,
        pincode_prefixes=payload.pincode_prefixes,
        status=payload.status,
        auto_created=False,
    )
    db.add(city)
    await db.commit()
    invalidate_verified_cache()
    return {"city": serialize_city(city)}


@router.patch(
    "/cities/{city_id}",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def update_city(
    city_id: str, payload: CityPayload, db: AsyncSession = Depends(get_db)
) -> dict:
    city = await db.get(City, city_id)
    if city is None:
        raise HTTPException(status_code=404, detail="city not found")
    city.name = payload.name.strip()
    city.state = payload.state
    city.aliases = payload.aliases
    city.pincode_prefixes = payload.pincode_prefixes
    city.status = payload.status
    await db.commit()
    invalidate_verified_cache()
    return {"city": serialize_city(city)}


@router.post(
    "/vendor-categories",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def create_vendor_category(
    payload: VendorCategoryPayload, db: AsyncSession = Depends(get_db)
) -> dict:
    slug = _slugify_category(payload.name)
    existing = await db.get(VendorCategory, slug)
    if existing is None:
        existing = VendorCategory(id=slug, name=payload.name.strip(), auto_created=True)
        db.add(existing)
        await db.commit()
    return {"category": {"id": existing.id, "name": existing.name, "auto_created": existing.auto_created}}


@router.patch(
    "/vendor-categories/{category_id}",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def rename_vendor_category(
    category_id: str, payload: VendorCategoryPayload, db: AsyncSession = Depends(get_db)
) -> dict:
    # Rename only changes the display label; the slug id (and all vendor links)
    # stay stable, so no relinking is needed.
    category = await db.get(VendorCategory, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="category not found")
    category.name = payload.name.strip()
    await db.commit()
    return {"category": {"id": category.id, "name": category.name, "auto_created": category.auto_created}}


@router.delete(
    "/vendor-categories/{category_id}",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def delete_vendor_category(category_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    category = await db.get(VendorCategory, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="category not found")
    in_use = (
        await db.execute(
            select(func.count())
            .select_from(VendorCategoryLink)
            .where(VendorCategoryLink.category_id == category_id)
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"category is used by {in_use} vendor(s); reassign them first",
        )
    await db.delete(category)
    await db.commit()
    return {"success": True}


@router.get("/vendors", dependencies=[Depends(require_role("owner", "ops", "vendor_success"))])
async def list_vendors(
    db: AsyncSession = Depends(get_db),
    x_admin_role: Annotated[str | None, Header()] = None,
    q: str | None = Query(default=None),
    onboarding_status: str | None = Query(default=None),
    vendor_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    all_vendors = (
        await db.execute(
            select(Vendor)
            .options(selectinload(Vendor.cities), selectinload(Vendor.category_links))
            .order_by(Vendor.name)
        )
    ).scalars().unique().all()
    if onboarding_status:
        all_vendors = [v for v in all_vendors if v.onboarding_status == onboarding_status]
    if vendor_type:
        all_vendors = [
            v for v in all_vendors if getattr(v, "vendor_type", "normal") == vendor_type
        ]
    if q:
        needle = q.lower()
        all_vendors = [
            v for v in all_vendors if needle in f"{v.name} {v.category}".lower()
        ]
    total = len(all_vendors)
    vendors = all_vendors[(page - 1) * limit : (page - 1) * limit + limit]
    role = x_admin_role or "owner"
    # Product counts only for the vendors on this page.
    page_ids = [v.id for v in vendors]
    counts: dict[str, int] = {}
    if page_ids:
        rows = await db.execute(
            select(Product.vendor_id, func.count(Product.id))
            .where(Product.vendor_id.in_(page_ids))
            .group_by(Product.vendor_id)
        )
        counts = {vendor_id: count for vendor_id, count in rows.all()}
    return {
        "vendors": [
            serialize_vendor(vendor, role=role, product_count=counts.get(vendor.id, 0))
            for vendor in vendors
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.post("/vendors", dependencies=[Depends(require_role("owner", "ops"))])
async def create_vendor(
    payload: VendorPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_role: Annotated[str | None, Header()] = None,
) -> dict:
    vendor = Vendor(id=generate_id("ven"), name=payload.name, category=payload.category or "")
    apply_vendor_payload(vendor, payload)
    db.add(vendor)
    await db.flush()
    await replace_vendor_cities(db, vendor, payload.city_ids)
    await replace_vendor_categories(db, vendor, payload.category_ids)
    await db.commit()
    await db.refresh(vendor, attribute_names=["cities", "category_links"])
    return {"vendor": serialize_vendor(vendor, role=x_admin_role or "owner")}


@router.patch("/vendors/{vendor_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def update_vendor(
    vendor_id: str,
    payload: VendorPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_role: Annotated[str | None, Header()] = None,
) -> dict:
    vendor = (
        await db.execute(
            select(Vendor)
            .where(Vendor.id == vendor_id)
            .options(selectinload(Vendor.cities), selectinload(Vendor.category_links))
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    apply_vendor_payload(vendor, payload)
    await replace_vendor_cities(db, vendor, payload.city_ids)
    await replace_vendor_categories(db, vendor, payload.category_ids)
    await db.commit()
    await db.refresh(vendor, attribute_names=["cities", "category_links"])
    return {"vendor": serialize_vendor(vendor, role=x_admin_role or "owner")}


class VendorNotifyPayload(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


@router.post(
    "/vendors/{vendor_id}/notify",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def notify_vendor(
    vendor_id: str,
    payload: VendorNotifyPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a free-text WhatsApp message to a vendor from the admin panel."""
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    if not vendor.phone:
        raise HTTPException(status_code=400, detail="vendor has no phone on file")

    from app.services.whatsapp import whatsapp_service

    try:
        await whatsapp_service.send_text(vendor.phone, payload.message)
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the admin UI
        raise HTTPException(
            status_code=502, detail=f"WhatsApp send failed: {str(exc)[:200]}"
        ) from exc
    return {"ok": True, "sent_to": vendor.phone}


@router.delete("/vendors/{vendor_id}", dependencies=[Depends(require_admin_token)])
async def delete_vendor(vendor_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    await db.delete(vendor)
    await db.commit()
    return {"ok": True}


# ── Vendor KYC documents ──────────────────────────────────────────────────────
# Fixed checklist (DOC_TYPES). Files go to a *private* R2 prefix and are served
# only via short-lived presigned URLs — they contain PII (PAN, bank details).

_DOC_PRESIGN_TTL = 300  # seconds a view link stays valid


def _r2_client():
    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
        ]
    ):
        raise HTTPException(status_code=503, detail="R2 is not configured")
    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )


def serialize_vendor_documents(documents: list[VendorDocument], client) -> list[dict]:
    by_type = {d.doc_type: d for d in documents}
    out = []
    for doc_type in DOC_TYPES:
        required = doc_type in REQUIRED_DOC_TYPES
        doc = by_type.get(doc_type)
        if doc is None:
            out.append({"doc_type": doc_type, "required": required, "uploaded": False})
            continue
        view_url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.r2_bucket, "Key": doc.file_key},
            ExpiresIn=_DOC_PRESIGN_TTL,
        )
        out.append(
            {
                "doc_type": doc_type,
                "required": required,
                "uploaded": True,
                "file_name": doc.file_name,
                "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
                "view_url": view_url,
            }
        )
    return out


def documents_complete(items: list[dict]) -> bool:
    """KYC is complete once every *required* document is uploaded."""
    return all(i["uploaded"] for i in items if i["required"])


async def get_vendor_or_404(db: AsyncSession, vendor_id: str) -> Vendor:
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")
    return vendor


@router.get(
    "/vendors/{vendor_id}/documents",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def list_vendor_documents(
    vendor_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_vendor_or_404(db, vendor_id)
    client = _r2_client()
    documents = (
        await db.execute(select(VendorDocument).where(VendorDocument.vendor_id == vendor_id))
    ).scalars().all()
    items = serialize_vendor_documents(list(documents), client)
    return {"documents": items, "complete": documents_complete(items)}


@router.post(
    "/vendors/{vendor_id}/documents/{doc_type}",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def upload_vendor_document(
    vendor_id: str,
    doc_type: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    from app.api.admin_settings import record_audit

    if doc_type not in DOC_TYPES:
        raise HTTPException(status_code=400, detail="invalid doc_type")
    await get_vendor_or_404(db, vendor_id)
    client = _r2_client()

    suffix = PurePosixPath(file.filename or "document").suffix.lower()[:12]
    key = f"vendor-docs/{vendor_id}/{doc_type}/{uuid4().hex}{suffix}"
    body = await file.read()
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=body,
        ContentType=file.content_type or "application/octet-stream",
    )

    existing = (
        await db.execute(
            select(VendorDocument).where(
                VendorDocument.vendor_id == vendor_id,
                VendorDocument.doc_type == doc_type,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-upload replaces the previous file; drop the old object from R2.
        old_key = existing.file_key
        existing.file_key = key
        existing.file_name = file.filename
        existing.uploaded_at = datetime.utcnow()
        try:
            client.delete_object(Bucket=settings.r2_bucket, Key=old_key)
        except Exception:
            pass  # best-effort cleanup; the DB row already points at the new key
    else:
        db.add(
            VendorDocument(
                id=generate_id("vdoc"),
                vendor_id=vendor_id,
                doc_type=doc_type,
                file_key=key,
                file_name=file.filename,
            )
        )

    await record_audit(
        db,
        actor=x_admin_email,
        action="vendor_document.upload",
        entity_type="vendor",
        entity_id=vendor_id,
        summary=f"uploaded {doc_type}",
    )
    await db.commit()

    documents = (
        await db.execute(select(VendorDocument).where(VendorDocument.vendor_id == vendor_id))
    ).scalars().all()
    items = serialize_vendor_documents(list(documents), client)
    return {"documents": items, "complete": documents_complete(items)}


@router.delete(
    "/vendors/{vendor_id}/documents/{doc_type}",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def delete_vendor_document(
    vendor_id: str,
    doc_type: str,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    from app.api.admin_settings import record_audit

    doc = (
        await db.execute(
            select(VendorDocument).where(
                VendorDocument.vendor_id == vendor_id,
                VendorDocument.doc_type == doc_type,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    client = _r2_client()
    try:
        client.delete_object(Bucket=settings.r2_bucket, Key=doc.file_key)
    except Exception:
        pass  # best-effort; remove the DB row regardless
    await db.delete(doc)
    await record_audit(
        db,
        actor=x_admin_email,
        action="vendor_document.delete",
        entity_type="vendor",
        entity_id=vendor_id,
        summary=f"deleted {doc_type}",
    )
    await db.commit()
    return {"ok": True}


@router.get("/products", dependencies=[Depends(require_admin_token)])
async def list_products(
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None),
    lifecycle_status: str | None = Query(default=None),
    vendor_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    all_products = (
        await db.execute(
            select(Product)
            .options(
                selectinload(Product.vendor),
                selectinload(Product.variants),
                selectinload(Product.addons),
            )
            .order_by(Product.name)
        )
    ).scalars().unique().all()
    if vendor_id:
        all_products = [p for p in all_products if p.vendor_id == vendor_id]
    if lifecycle_status:
        all_products = [
            p for p in all_products if getattr(p, "lifecycle_status", "active") == lifecycle_status
        ]
    if q:
        needle = q.lower()
        all_products = [
            p for p in all_products
            if needle in f"{p.name} {p.vendor.name if p.vendor else ''}".lower()
        ]
    total = len(all_products)
    products = all_products[(page - 1) * limit : (page - 1) * limit + limit]
    return {
        "products": [serialize_product(product, include_vendor_name=True) for product in products],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.post("/products/{product_id}/preview-token", dependencies=[Depends(require_admin_token)])
async def create_product_preview_token(
    product_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Mint a read-only storefront preview token for one existing product."""
    if await db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="product not found")
    token, expires_at = mint_preview_token(product_id)
    return {"token": token, "expires_at": expires_at}


@router.post("/products", dependencies=[Depends(require_admin_token)])
async def create_product(payload: ProductPayload, db: AsyncSession = Depends(get_db)) -> dict:
    if await db.get(Vendor, payload.vendor_id) is None:
        raise HTTPException(status_code=400, detail="vendor_id does not exist")
    product = Product(id=generate_id("prd"), vendor_id=payload.vendor_id, variants=[], addons=[])
    db.add(product)
    await apply_product_payload(db, product, payload)
    await db.commit()
    product = await get_product_or_404(db, product.id)
    return {"product": serialize_product(product, include_vendor_name=True)}


async def apply_product_payload(db: AsyncSession, product: Product, payload: ProductPayload) -> None:
    product.vendor_id = payload.vendor_id
    product.name = payload.name
    product.description = sanitize_html(payload.description)
    product.category = payload.category
    product.occasions = payload.occasions
    product.preparation_time = payload.preparation_time
    product.dispatch_lead_time_days = payload.dispatch_lead_time_days
    product.delivery_pincodes = payload.delivery_pincodes
    product.ships_nationally = payload.ships_nationally
    product.is_bestseller = payload.is_bestseller
    product.product_images = payload.product_images
    product.care_instructions = payload.care_instructions
    product.whats_included = payload.whats_included
    product.customization_schema = payload.customization_schema
    # Customisable is derived: explicit flag OR a non-empty customization schema.
    product.is_customisable = payload.is_customisable or bool(payload.customization_schema)
    product.supports_gift_message = payload.supports_gift_message
    product.base_price_paise = payload.base_price_paise
    # Keep backwards compatibility with admin clients deployed before the
    # discounted-price and exact-inventory fields: omission means preserve,
    # explicit null means clear (base price / unlimited inventory).
    if "discounted_price_paise" in payload.model_fields_set:
        product.discounted_price_paise = payload.discounted_price_paise
    if "inventory_quantity" in payload.model_fields_set:
        product.inventory_quantity = payload.inventory_quantity
    product.gst_rate = payload.gst_rate
    product.hsn = payload.hsn
    # Delivery is priced once at order level. Accept legacy client fields for
    # backwards compatibility, but never persist a product-level surcharge.
    product.shipping_included = True
    product.shipping_fee_paise = None
    product.image_url = payload.image_url
    product.video_url = payload.video_url
    product.in_stock = payload.in_stock and payload.inventory_quantity != 0
    # Lifecycle is the richer state; keep the storefront `active` bool in sync.
    # If a lifecycle_status is provided, it wins; otherwise derive from `active`.
    if payload.lifecycle_status:
        product.lifecycle_status = payload.lifecycle_status
        product.active = payload.lifecycle_status == "active"
    else:
        product.active = payload.active
        product.lifecycle_status = "active" if payload.active else "paused"
    await db.flush()
    await sync_product_children(db, product, payload.variants, payload.addons)


@router.patch("/products/{product_id}", dependencies=[Depends(require_admin_token)])
async def update_product(
    product_id: str,
    payload: ProductPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if await db.get(Vendor, payload.vendor_id) is None:
        raise HTTPException(status_code=400, detail="vendor_id does not exist")
    product = await get_product_or_404(db, product_id)
    await apply_product_payload(db, product, payload)
    await db.commit()
    product = await get_product_or_404(db, product.id)
    return {"product": serialize_product(product, include_vendor_name=True)}


@router.delete("/products/{product_id}", dependencies=[Depends(require_admin_token)])
async def delete_product(product_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    # order_line_items.product_id is ondelete=RESTRICT — a product that's ever
    # been ordered must not vanish (it would orphan order history). Block it with
    # a clear message instead of letting the FK raise a raw 500; pause the product
    # via lifecycle_status instead of deleting.
    ordered = (
        await db.execute(
            select(func.count())
            .select_from(OrderLineItem)
            .where(OrderLineItem.product_id == product_id)
        )
    ).scalar_one()
    if ordered:
        raise HTTPException(
            status_code=409,
            detail="This product has orders and can't be deleted. Pause it instead.",
        )
    await db.delete(product)
    await db.commit()
    return {"ok": True}


@router.post("/products/{product_id}/image", dependencies=[Depends(require_admin_token)])
async def upload_product_image(
    product_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    product = await get_product_or_404(db, product_id)
    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise HTTPException(status_code=503, detail="R2 is not configured")

    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc

    suffix = PurePosixPath(file.filename or "image").suffix.lower()[:12] or ".jpg"
    key = f"products/{product_id}/{uuid4().hex}{suffix}"
    body = await file.read()

    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=body,
        ContentType=file.content_type or "application/octet-stream",
    )

    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    product.image_url = public_url
    await db.commit()
    product = await get_product_or_404(db, product_id)
    return {"image_url": public_url, "product": serialize_product(product, include_vendor_name=True)}
