import base64
import binascii
import hmac
import json
import logging
import time
from decimal import Decimal
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.session import get_db
from app.models.catalog_taxonomy import GiftingOccasion, ProductCategory
from app.models.product import Product
from app.models.vendor import Vendor, VendorCity
from app.services.delivery_estimate import earliest_delivery_date
from app.services.bouquet_pricing import (
    bouquet_recipe_cost,
    effective_variant_prices,
    public_bouquet_composition,
)
from app.services.storefront_pricing import StorefrontPricing, get_storefront_pricing
from app.utils.redis_client import get_redis
from app.utils.serviceability import check_product_serviceability


def _verify_preview_token(token: str, product_id: str) -> None:
    """Validate a short-lived, signed product-preview token.

    Token format: base64url("<product_id>|<expiry_unix>") + "." + HMAC-SHA256.
    Signed by the admin app with the shared preview_signing_secret. Grants read
    of exactly one product until it expires — never admin write access.
    """
    secret = settings.preview_signing_secret
    if not secret:
        raise HTTPException(status_code=503, detail="preview is not configured")
    dot = token.rfind(".")
    if dot < 0:
        raise HTTPException(status_code=403, detail="forbidden")
    payload, sig = token[:dot], token[dot + 1 :]
    expected = hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()
    if len(sig) != len(expected) or not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        # The admin signs with Node's base64url (no "=" padding); restore it
        # before decoding, since urlsafe_b64decode requires correct padding.
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        token_product_id, expiry_str = decoded.split("|")
        expiry = int(expiry_str)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=403, detail="forbidden")
    if token_product_id != product_id:
        raise HTTPException(status_code=403, detail="forbidden")
    if expiry < int(time.time()):
        raise HTTPException(status_code=403, detail="preview link expired")

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


logger = logging.getLogger("wishkr.catalog")

router = APIRouter(prefix="/catalog", tags=["catalog"])

PRODUCT_PREVIEW_TTL_SECONDS = 15 * 60


def mint_preview_token(product_id: str, *, now: int | None = None) -> tuple[str, int]:
    """Mint a short-lived token using the verifier's own configured secret.

    Keeping minting and verification in the backend removes the cross-service
    secret drift that can otherwise make every admin preview link invalid.
    """
    secret = settings.preview_signing_secret
    if not secret:
        raise HTTPException(status_code=503, detail="preview is not configured")
    expires_at = (int(time.time()) if now is None else now) + PRODUCT_PREVIEW_TTL_SECONDS
    payload = base64.urlsafe_b64encode(f"{product_id}|{expires_at}".encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{signature}", expires_at


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _discount_pct(base_paise: int, discounted_paise: int | None) -> str:
    if discounted_paise is None or discounted_paise >= base_paise or base_paise <= 0:
        return "0"
    discount = (
        Decimal(base_paise - discounted_paise) * Decimal(100)
    ) / Decimal(base_paise)
    return str(discount.quantize(Decimal("0.01")))


def serialize_pricing(pricing: StorefrontPricing | None) -> dict:
    # An active flash applies these global multipliers to the product's
    # original/base amount. While inactive they are neutral and the storefront
    # uses the product's own discounted price. Checkout recomputes the aggregate
    # authoritatively in product_orders.py.
    p = pricing
    return {
        "flash_active": bool(p.flash_active) if p else False,
        "flash_off_pct": _money(p.flash_off_pct) if p else "0",
        # list = struck-through price; charge = what the customer actually pays.
        "list_multiplier": _money(p.list_multiplier) if p else "1",
        "charge_multiplier": _money(p.charge_multiplier) if p else "1",
    }


def _serialize_variant(variant, *, include_costs: bool) -> dict:
    effective_base, effective_discount, seasonal_active = effective_variant_prices(
        variant
    )
    recipe = getattr(variant, "bouquet_recipe", None)
    body = {
        "id": variant.id,
        "name": variant.name,
        "sku": variant.sku,
        # Stored normal amounts remain available to the admin editor.
        "price_delta_paise": variant.price_delta_paise,
        "discounted_price_paise": getattr(
            variant, "discounted_price_paise", None
        ),
        # Buyer/checkout-effective amounts account for the current seasonal
        # window. Clients must use these when present.
        "effective_base_price_paise": effective_base,
        "effective_discounted_price_paise": effective_discount,
        "seasonal_price_active": seasonal_active,
        "discount_pct": _discount_pct(effective_base, effective_discount),
        "inventory_quantity": getattr(variant, "inventory_quantity", None),
        "in_stock": getattr(variant, "inventory_quantity", None) != 0,
        "active": variant.active,
        "sort_order": variant.sort_order,
        "bouquet_composition": public_bouquet_composition(recipe),
    }
    if include_costs:
        body.update(
            {
                "seasonal_price_paise": getattr(
                    variant, "seasonal_price_paise", None
                ),
                "seasonal_price_start": (
                    variant.seasonal_price_start.isoformat()
                    if getattr(variant, "seasonal_price_start", None)
                    else None
                ),
                "seasonal_price_end": (
                    variant.seasonal_price_end.isoformat()
                    if getattr(variant, "seasonal_price_end", None)
                    else None
                ),
                "bouquet_recipe": recipe,
                "bouquet_cost_summary": {
                    "off_season_paise": bouquet_recipe_cost(recipe),
                    "peak_season_paise": bouquet_recipe_cost(recipe, peak=True),
                }
                if recipe
                else None,
            }
        )
    return body


def serialize_product(
    product: Product,
    *,
    include_vendor_name: bool = False,
    pricing: StorefrontPricing | None = None,
) -> dict:
    return {
        "id": product.id,
        "vendor_id": product.vendor_id,
        "pricing": serialize_pricing(pricing),
        # Vendor business name is intentionally not exposed on the storefront to
        # avoid buyers contacting vendors directly and bypassing Wishkr. Admin
        # callers opt in via include_vendor_name — they need to know the supplier.
        "vendor_name": (product.vendor.name if product.vendor else None) if include_vendor_name else None,
        # A configured flat fee is the complete order shipping charge regardless
        # of quantity. ``None`` means the storefront should use the legacy
        # product base + per-extra-unit calculation.
        "vendor_shipping_flat_fee_paise": (
            product.vendor.shipping_flat_fee_paise if product.vendor else None
        ),
        "vendor_shipping_extra_item_fee_paise": (
            product.vendor.shipping_extra_item_fee_paise if product.vendor else 0
        ),
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "occasions": product.occasions or [],
        "preparation_time": product.preparation_time,
        "dispatch_lead_time_days": product.dispatch_lead_time_days,
        "delivery_pincodes": product.delivery_pincodes or [],
        "ships_nationally": getattr(product, "ships_nationally", True),
        "is_bestseller": product.is_bestseller,
        "is_customisable": product.is_customisable,
        "supports_gift_message": getattr(product, "supports_gift_message", False),
        "product_images": product.product_images or [],
        "care_instructions": product.care_instructions,
        "whats_included": product.whats_included,
        "customization_schema": product.customization_schema or [],
        "base_price_paise": product.base_price_paise,
        "discounted_price_paise": getattr(product, "discounted_price_paise", None),
        "discount_pct": _discount_pct(
            product.base_price_paise,
            getattr(product, "discounted_price_paise", None),
        ),
        "inventory_quantity": getattr(product, "inventory_quantity", None),
        "gst_rate": _money(product.gst_rate),
        "hsn": product.hsn,
        "shipping_included": product.shipping_included,
        "shipping_fee_paise": product.shipping_fee_paise,
        "image_url": product.image_url,
        "video_url": product.video_url,
        "active": product.active,
        "in_stock": (
            getattr(product, "in_stock", True) is not False
            and getattr(product, "inventory_quantity", None) != 0
        ),
        "lifecycle_status": getattr(product, "lifecycle_status", "active"),
        "earliest_delivery_date": (
            earliest_delivery_date(
                product.vendor, product_lead_time_days=product.dispatch_lead_time_days
            ).isoformat()
            if product.vendor
            else None
        ),
        "variants": [
            _serialize_variant(variant, include_costs=include_vendor_name)
            for variant in sorted(product.variants, key=lambda item: item.sort_order)
            if include_vendor_name or variant.active
        ],
        "addons": [
            {
                "id": addon.id,
                "name": addon.name,
                "price_paise": addon.price_paise,
                "active": addon.active,
                "sort_order": addon.sort_order,
                "allow_quantity": addon.allow_quantity,
                "min_quantity": addon.min_quantity,
                "max_quantity": addon.max_quantity,
            }
            for addon in sorted(product.addons, key=lambda item: item.sort_order)
            if addon.active
        ],
    }


TAXONOMY_CACHE_KEY = "catalog:taxonomy"
TAXONOMY_CACHE_TTL_SECONDS = 300

# The storefront product list. Every render of /shop and each facet page reads
# it, and it is the heaviest query in the API (all active products + vendor,
# variants and addons eagerly loaded).
#
# TTL is deliberately short: the payload embeds the storefront pricing
# multipliers, so a stale entry shows a stale price. 60s bounds that even if an
# invalidation is ever missed, and admin/vendor writes call
# invalidate_product_cache() to drop it immediately.
#
# NOT cached: /pricing (checkout must read live rates — see that endpoint) and
# the per-product detail route, which is cheap and price-sensitive.
PRODUCTS_CACHE_KEY = "catalog:products:active"
PRODUCTS_CACHE_TTL_SECONDS = 60


async def invalidate_product_cache() -> None:
    """Drop the cached storefront product list.

    Call after any write that changes what /products returns: product fields,
    variants, addons, vendor active/name, or the storefront pricing settings.
    Redis absent or erroring is not an error — the TTL still bounds staleness.
    """
    redis = get_redis()
    if redis is None:
        return
    try:
        await redis.delete(PRODUCTS_CACHE_KEY)
    except (RedisError, OSError):
        logger.warning("product_cache_invalidate_failed", exc_info=True)


@router.get("/taxonomy")
async def get_taxonomy(db: AsyncSession = Depends(get_db)) -> dict:
    """Product categories + gifting occasions for filters and editors.

    The DB is the source of truth (product_categories / gifting_occasions,
    seeded by migration); Redis caches the payload so storefront renders and
    editor loads don't each hit the DB. Follows the redis_client rules: no
    Redis configured or a Redis error → serve straight from the DB.
    """
    redis = get_redis()
    if redis is not None:
        try:
            cached = await redis.get(TAXONOMY_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except (RedisError, OSError):
            pass

    categories = (
        (
            await db.execute(
                select(ProductCategory).order_by(
                    ProductCategory.sort_order, ProductCategory.name
                )
            )
        )
        .scalars()
        .all()
    )
    occasions = (
        (
            await db.execute(
                select(GiftingOccasion).order_by(
                    GiftingOccasion.sort_order, GiftingOccasion.name
                )
            )
        )
        .scalars()
        .all()
    )
    payload = {
        "categories": [category.name for category in categories],
        "occasions": [{"label": occ.name, "slug": occ.id} for occ in occasions],
    }
    if redis is not None:
        try:
            await redis.set(
                TAXONOMY_CACHE_KEY, json.dumps(payload), ex=TAXONOMY_CACHE_TTL_SECONDS
            )
        except (RedisError, OSError):
            pass
    return payload


@router.get("/products")
async def list_active_products(db: AsyncSession = Depends(get_db)) -> dict:
    """Active storefront catalogue, Redis-cached behind a short TTL.

    Same rules as the taxonomy cache: no Redis configured or a Redis error →
    serve straight from the DB. Writes call invalidate_product_cache(), and the
    60s TTL bounds staleness if one is ever missed.
    """
    redis = get_redis()
    if redis is not None:
        try:
            cached = await redis.get(PRODUCTS_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except (RedisError, OSError):
            pass

    result = await db.execute(
        select(Product)
        .join(Vendor)
        .where(Product.active.is_(True), Vendor.active.is_(True))
        .options(
            selectinload(Product.vendor),
            selectinload(Product.variants),
            selectinload(Product.addons),
        )
        .order_by(Product.is_bestseller.desc(), Product.name, Product.id)
    )
    products = result.scalars().unique().all()
    pricing = await get_storefront_pricing(db)
    payload = {
        "products": [serialize_product(product, pricing=pricing) for product in products]
    }

    if redis is not None:
        try:
            await redis.set(
                PRODUCTS_CACHE_KEY, json.dumps(payload), ex=PRODUCTS_CACHE_TTL_SECONDS
            )
        except (RedisError, OSError):
            pass
    return payload


@router.get("/pricing")
async def get_pricing_rates(db: AsyncSession = Depends(get_db)) -> dict:
    """Live storefront markup/flash rates. The web cart/checkout read this fresh
    (uncached) so the customer-facing CHARGE total always matches what the
    backend collects — product listings may lag by their cache, but the amount
    the buyer authorises must not."""
    return {"pricing": serialize_pricing(await get_storefront_pricing(db))}


@router.get("/products/{product_id}")
async def get_active_product(product_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    product = (
        await db.execute(
            select(Product)
            .join(Vendor)
            .where(
                Product.id == product_id,
                Product.active.is_(True),
                Vendor.active.is_(True),
            )
            .options(
                selectinload(Product.vendor),
                selectinload(Product.variants),
                selectinload(Product.addons),
            )
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    pricing = await get_storefront_pricing(db)
    return {"product": serialize_product(product, pricing=pricing)}


@router.get("/products/{product_id}/preview")
async def get_product_preview(
    product_id: str, token: str = "", db: AsyncSession = Depends(get_db)
) -> dict:
    """Fetch a product regardless of active/lifecycle status, for admin preview
    of draft/paused listings on the real storefront PDP. Authorised by a
    short-lived signed token (not the admin API token)."""
    _verify_preview_token(token, product_id)
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
    pricing = await get_storefront_pricing(db)
    return {"product": serialize_product(product, pricing=pricing)}


@router.get("/products/{product_id}/pincode-check")
async def check_pincode(
    product_id: str, pincode: str, db: AsyncSession = Depends(get_db), token: str = ""
) -> dict:
    """Whether ``product`` delivers to ``pincode``.

    Resolution order:

    1. If the product lists explicit ``delivery_pincodes``, that list is
       authoritative (a finer per-product override).
    2. Otherwise serviceability follows the product's reach: national products
       deliver everywhere; local products deliver only where the vendor operates
       (matched by the pincode's prefix against the vendor's cities).

    A valid preview ``token`` (same as ``/preview``) bypasses the active/
    lifecycle filter, so admins/vendors can test delivery on paused listings.
    """
    pincode = pincode.strip()
    if not (len(pincode) == 6 and pincode.isdigit()):
        raise HTTPException(status_code=422, detail="pincode must be 6 digits")

    if token:
        _verify_preview_token(token, product_id)
        product = (
            await db.execute(select(Product).where(Product.id == product_id))
        ).scalar_one_or_none()
    else:
        product = (
            await db.execute(
                select(Product)
                .join(Vendor)
                .where(
                    Product.id == product_id,
                    Product.active.is_(True),
                    Vendor.active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")

    listed = product.delivery_pincodes or []
    if listed:
        serviceable = pincode in listed
    else:
        vendor_city_ids = (
            await db.execute(
                select(VendorCity.city_id).where(VendorCity.vendor_id == product.vendor_id)
            )
        ).scalars().all()
        result = await check_product_serviceability(
            db,
            ships_nationally=product.ships_nationally,
            vendor_city_ids=list(vendor_city_ids),
            dest_city=None,
            dest_pincode=pincode,
        )
        serviceable = result.serviceable
    return {"serviceable": serviceable, "pincode": pincode}


@router.post("/products/{product_id}/customization-upload")
async def upload_customization_file(
    product_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Buyer-facing upload for a customization field (e.g. a photo for a printed mug).

    Stores the file in the same R2 bucket as admin product images, under a
    per-product prefix, and returns its public URL — which the buyer then
    stores as the value of the relevant ``customizations`` key, identical to
    typed text. No DB row references this upload directly; orphans from
    abandoned carts are tolerated for now.
    """
    product = (
        await db.execute(
            select(Product).where(Product.id == product_id, Product.active.is_(True))
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")

    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise HTTPException(status_code=503, detail="uploads are not configured")

    content_type = (file.content_type or "").lower()
    suffix = ALLOWED_UPLOAD_TYPES.get(content_type)
    if suffix is None:
        raise HTTPException(
            status_code=422,
            detail="only JPEG, PNG, WEBP, or PDF files are accepted",
        )

    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file must be 10 MB or smaller")

    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc

    # Ignore the client-supplied filename entirely — never trust it for the key.
    _ = PurePosixPath(file.filename or "")
    key = f"customizations/{product_id}/{uuid4().hex}{suffix}"

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
        ContentType=content_type,
    )

    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    return {"url": public_url}
