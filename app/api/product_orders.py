import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models import Order, OrderStatus, User
from app.models.order_line_item import OrderLineItem
from app.models.product import Product, ProductVariant
from app.models.vendor import Vendor
from app.models.vendor import VendorCity
from app.services.bouquet_pricing import effective_variant_prices
from app.services.delivery_estimate import earliest_delivery_date
from app.services.razorpay_service import razorpay_service
from app.services.storefront_pricing import get_storefront_pricing
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter
from app.utils.serviceability import check_product_serviceability

logger = logging.getLogger("wishkr.product_orders")

# /product/quote is unauthenticated and does one DB lookup per line (up to 20)
# plus a pricing fetch, so an unthrottled caller can amplify cheaply. 30/min is
# far above real shopper behaviour — the cart re-quotes on edit, not per
# keystroke — while capping the amplification.
_quote_rate_limiter = RateLimiter(max_requests=30, window_seconds=60, name="product_quote")


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


router = APIRouter(prefix="/orders", tags=["orders"])


class SelectedAddonPayload(BaseModel):
    addon_id: str = Field(min_length=1)
    # strict=True rejects bool/float/string coercion — a decimal, "3", null, or
    # True must all fail rather than be silently coerced to an int.
    quantity: int = Field(strict=True)
    note: str | None = Field(default=None, max_length=500)


class ProductOrderItemPayload(BaseModel):
    product_id: str
    variant_id: str | None = None
    selected_addons: list[SelectedAddonPayload] = Field(default_factory=list)
    customizations: dict = Field(default_factory=dict)
    qty: int = Field(ge=1, le=100)


class ProductOrderQuotePayload(BaseModel):
    items: list[ProductOrderItemPayload] = Field(min_length=1, max_length=20)


class CustomerPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=5, max_length=20)
    email: str | None = Field(default=None, max_length=255)


class ShippingAddressPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=20)
    line1: str = Field(min_length=1, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=1, max_length=80)
    pincode: str = Field(pattern=r"^\d{6}$")


class AttributionTouchPayload(BaseModel):
    utm_source: str | None = Field(default=None, max_length=200)
    utm_medium: str | None = Field(default=None, max_length=200)
    utm_campaign: str | None = Field(default=None, max_length=200)
    utm_content: str | None = Field(default=None, max_length=200)
    utm_term: str | None = Field(default=None, max_length=200)
    landing_path: str = Field(max_length=1000)
    referrer: str | None = Field(default=None, max_length=500)
    captured_at: str = Field(max_length=50)


class CampaignAttributionPayload(BaseModel):
    first_touch: AttributionTouchPayload | None = None
    last_touch: AttributionTouchPayload | None = None


class ProductOrderPayload(BaseModel):
    customer: CustomerPayload
    shipping_address: ShippingAddressPayload
    items: list[ProductOrderItemPayload] = Field(min_length=1, max_length=20)
    delivery_date: date
    gift_message: str | None = Field(default=None, max_length=500)
    is_gift: bool = True
    hide_prices: bool = True
    attribution: CampaignAttributionPayload | None = None


class VerifyPaymentPayload(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class PricedLine(BaseModel):
    product: Product
    variant: ProductVariant | None
    # Frozen snapshot of the chosen add-ons, ready to persist on the line item:
    #   {addon_id, name, unit_price_paise, quantity, total_price_paise, note?}
    selected_addons: list[dict]
    customizations: dict
    qty: int
    # Vendor's normal discounted unit price, including add-ons/customisations.
    unit_price_paise: int
    # Required so every checkout caller explicitly supplies the original/base
    # amount used by normal list-price and active flash calculations.
    base_unit_price_paise: int
    line_total_paise: int
    shipping_fee_paise: int

    model_config = {"arbitrary_types_allowed": True}


@dataclass(frozen=True)
class ProductOrderQuote:
    priced_lines: list[PricedLine]
    vendor: Vendor
    base_product_total_paise: int
    raw_product_total_paise: int
    product_subtotal_paise: int
    product_list_subtotal_paise: int
    discount_paise: int
    shipping_paise: int
    total_paise: int
    earliest_delivery_date: date
    line_earliest_delivery_dates: list[date]
    line_product_subtotals_paise: list[int]
    line_list_subtotals_paise: list[int]
    line_discounts_paise: list[int]


async def get_or_create_customer(db: AsyncSession, customer: CustomerPayload) -> User:
    user = (
        await db.execute(select(User).where(User.phone == customer.phone))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            id=generate_id("usr"),
            phone=customer.phone,
            name=customer.name,
            email=customer.email,
            is_guest=True,
        )
        db.add(user)
        await db.flush()
        return user

    user.name = customer.name or user.name
    if customer.email:
        user.email = customer.email
    db.add(user)
    await db.flush()
    return user


async def price_line(db: AsyncSession, item: ProductOrderItemPayload) -> PricedLine:
    product = (
        await db.execute(
            select(Product)
            .join(Vendor)
            .where(
                Product.id == item.product_id,
                Product.active.is_(True),
                Vendor.active.is_(True),
            )
            .options(
                selectinload(Product.variants),
                selectinload(Product.addons),
                selectinload(Product.vendor),
            )
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status_code=400, detail=f"product {item.product_id} is unavailable")
    # Out-of-stock is a vendor-controlled, instant block on buying — enforced
    # server-side so it holds even if a client bypasses the disabled UI.
    if not getattr(product, "in_stock", True):
        raise HTTPException(
            status_code=409, detail=f"'{product.name}' is currently out of stock"
        )
    if getattr(product, "inventory_quantity", None) == 0:
        raise HTTPException(
            status_code=409, detail=f"'{product.name}' is currently out of stock"
        )

    variant: ProductVariant | None = None
    if item.variant_id:
        variant = next(
            (candidate for candidate in product.variants if candidate.id == item.variant_id),
            None,
        )
        if variant is None or not variant.active:
            raise HTTPException(status_code=400, detail=f"variant {item.variant_id} is unavailable")
        if getattr(variant, "inventory_quantity", None) == 0:
            raise HTTPException(
                status_code=409,
                detail=f"variant '{variant.name}' is currently out of stock",
            )

    # Resolve and price the selected add-ons. Every figure (name, unit price) is
    # taken from the DB row, never from the client — the client only supplies the
    # add-on id, quantity, and an optional note.
    addon_lookup = {addon.id: addon for addon in product.addons if addon.active}
    selected_addons: list[dict] = []
    seen_addon_ids: set[str] = set()
    addon_total_paise = 0
    for selected in item.selected_addons:
        if selected.addon_id in seen_addon_ids:
            raise HTTPException(
                status_code=400,
                detail=f"add-on {selected.addon_id} is selected more than once",
            )
        seen_addon_ids.add(selected.addon_id)

        addon = addon_lookup.get(selected.addon_id)
        if addon is None:
            # Covers unknown ids, inactive add-ons, and add-ons from another
            # product (addon_lookup only holds this product's active add-ons).
            raise HTTPException(
                status_code=400, detail=f"addon {selected.addon_id} is unavailable"
            )

        quantity = selected.quantity
        if not addon.allow_quantity:
            if quantity != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"add-on '{addon.name}' does not support a quantity",
                )
        else:
            if quantity < addon.min_quantity or quantity > addon.max_quantity:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"add-on '{addon.name}' quantity must be between "
                        f"{addon.min_quantity} and {addon.max_quantity}"
                    ),
                )

        line_total = addon.price_paise * quantity
        addon_total_paise += line_total
        snapshot = {
            "addon_id": addon.id,
            "name": addon.name,
            "unit_price_paise": addon.price_paise,
            "quantity": quantity,
            "total_price_paise": line_total,
        }
        if selected.note and selected.note.strip():
            snapshot["note"] = selected.note.strip()
        selected_addons.append(snapshot)

    customization_total_paise = 0
    for option in product.customization_schema or []:
        name = option.get("name")
        if name and item.customizations.get(name):
            customization_total_paise += int(option.get("price_delta_paise") or 0)

    # A selected variant's price is ABSOLUTE — it replaces the base price, not
    # added to it. `price_delta_paise` holds that absolute amount when a variant
    # is chosen (legacy column name). Base price applies only when no variant is
    # selected. Add-ons and customisation surcharges stack on top either way.
    if variant:
        (
            base_product_paise,
            discounted_product_paise,
            _seasonal_price_active,
        ) = effective_variant_prices(variant)
    else:
        base_product_paise = product.base_price_paise
        discounted_product_paise = getattr(product, "discounted_price_paise", None)
    if discounted_product_paise is None:
        discounted_product_paise = base_product_paise
    extras_paise = addon_total_paise + customization_total_paise
    unit_price_paise = (
        int(discounted_product_paise)
        + extras_paise
    )
    # Add-ons/customisations are included equally in the normal base and
    # discounted amounts. During a flash campaign the global formula applies
    # to the complete base unit amount, preserving the existing checkout model.
    base_unit_price_paise = (
        base_product_paise
        + extras_paise
    )
    # The product's own shipping fee. Order-level shipping is charged once (the
    # highest fee across lines, see create_product_order), so it is NOT baked
    # into line_total_paise here — only the product portion is.
    shipping_fee_paise = (
        0 if product.shipping_included else int(product.shipping_fee_paise or 0)
    )
    return PricedLine(
        product=product,
        variant=variant,
        selected_addons=selected_addons,
        customizations=item.customizations,
        qty=item.qty,
        unit_price_paise=unit_price_paise,
        base_unit_price_paise=base_unit_price_paise,
        line_total_paise=unit_price_paise * item.qty,
        shipping_fee_paise=shipping_fee_paise,
    )


def compute_order_shipping_paise(priced_lines: list[PricedLine], vendor: Vendor) -> int:
    """Return this single-vendor order's complete shipping charge.

    A configured vendor flat fee overrides product and quantity-based shipping.
    Otherwise, preserve the legacy highest-product-fee + extra-unit calculation.
    """
    flat_fee_paise = getattr(vendor, "shipping_flat_fee_paise", None)
    if flat_fee_paise is not None:
        return int(flat_fee_paise)

    base = max((line.shipping_fee_paise for line in priced_lines), default=0)
    total_units = sum(line.qty for line in priced_lines)
    extra_units = max(0, total_units - 1)
    return base + extra_units * (vendor.shipping_extra_item_fee_paise or 0)


def allocate_whole_rupee_total(total_paise: int, weights: list[int]) -> list[int]:
    """Allocate an aggregate whole-rupee total without changing that total.

    Storefront pricing rounds once at order level. Allocating its final result
    lets checkout show authoritative per-line amounts whose sum exactly matches
    the backend subtotal, without re-running pricing in the browser.
    """
    if not weights:
        return []
    if total_paise < 0 or total_paise % 100:
        raise ValueError("allocated storefront totals must be non-negative whole rupees")
    weight_total = sum(weights)
    if weight_total <= 0:
        return [0] * len(weights)

    total_rupees = total_paise // 100
    quotients_and_remainders = [divmod(total_rupees * weight, weight_total) for weight in weights]
    allocated_rupees = [quotient for quotient, _ in quotients_and_remainders]
    remainder_rupees = total_rupees - sum(allocated_rupees)
    remainder_order = sorted(
        range(len(weights)),
        key=lambda index: (-quotients_and_remainders[index][1], index),
    )
    for index in remainder_order[:remainder_rupees]:
        allocated_rupees[index] += 1
    return [amount * 100 for amount in allocated_rupees]


def enforce_allocation_minimums(
    allocated_paise: list[int],
    minimums_paise: list[int],
) -> list[int]:
    """Rebalance an allocation so every entry meets its minimum.

    Separate base-price and discounted-price weights can assign a rounding rupee
    to different lines. Move whole rupees between lines without changing the
    aggregate list total, ensuring no displayed line has a negative discount.
    """
    if len(allocated_paise) != len(minimums_paise):
        raise ValueError("allocation and minimum lengths must match")
    result = allocated_paise.copy()
    for receiver, minimum in enumerate(minimums_paise):
        needed = minimum - result[receiver]
        if needed <= 0:
            continue
        for donor, donor_minimum in enumerate(minimums_paise):
            movable = result[donor] - donor_minimum
            if movable <= 0:
                continue
            moved = min(needed, movable)
            result[donor] -= moved
            result[receiver] += moved
            needed -= moved
            if needed == 0:
                break
        if needed:
            # ponytail: this means an upstream pricing invariant was violated;
            # it is an application bug, not invalid customer input.
            raise RuntimeError("list total is below the charged total")
    return result


def validate_inventory(priced_lines: list[PricedLine]) -> None:
    """Reject a quote whose combined lines exceed a finite stock quantity.

    The same product/variant can appear in several cart lines because add-ons
    and customisations create separate lines, so checks must aggregate first.
    Null inventory intentionally means unlimited/untracked.
    """

    product_totals: dict[str, int] = {}
    variant_totals: dict[str, int] = {}
    variants: dict[str, ProductVariant] = {}
    products: dict[str, Product] = {}
    for line in priced_lines:
        product_totals[line.product.id] = product_totals.get(line.product.id, 0) + line.qty
        products[line.product.id] = line.product
        if line.variant is not None:
            variant_totals[line.variant.id] = variant_totals.get(line.variant.id, 0) + line.qty
            variants[line.variant.id] = line.variant

    for product_id, requested in product_totals.items():
        product = products[product_id]
        available = getattr(product, "inventory_quantity", None)
        if available is not None and requested > available:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Only {available} unit(s) of '{product.name}' are currently available"
                ),
            )

    for variant_id, requested in variant_totals.items():
        variant = variants[variant_id]
        available = getattr(variant, "inventory_quantity", None)
        if available is not None and requested > available:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Only {available} unit(s) of variant '{variant.name}' are currently "
                    "available"
                ),
            )


async def build_product_order_quote(
    db: AsyncSession,
    items: list[ProductOrderItemPayload],
) -> ProductOrderQuote:
    """Price and date a cart once on the backend for display and order creation."""
    priced_lines = [await price_line(db, item) for item in items]
    validate_inventory(priced_lines)
    vendor_ids = {line.product.vendor_id for line in priced_lines}
    if len(vendor_ids) != 1:
        raise HTTPException(status_code=400, detail="product orders must contain one vendor")

    vendor = priced_lines[0].product.vendor
    base_product_total_paise = sum(
        line.base_unit_price_paise * line.qty for line in priced_lines
    )
    raw_product_total_paise = sum(line.line_total_paise for line in priced_lines)
    shipping_paise = compute_order_shipping_paise(priced_lines, vendor)
    pricing = await get_storefront_pricing(db)
    product_subtotal_paise = pricing.charge_paise(
        base_product_total_paise,
        raw_product_total_paise,
    )

    charge_weights = [
        (
            line.base_unit_price_paise
            if pricing.flash_active
            else line.unit_price_paise
        )
        * line.qty
        for line in priced_lines
    ]
    line_product_subtotals_paise = allocate_whole_rupee_total(
        product_subtotal_paise,
        charge_weights,
    )
    list_weights = [
        line.base_unit_price_paise * line.qty for line in priced_lines
    ]
    line_list_subtotals_paise = enforce_allocation_minimums(
        allocate_whole_rupee_total(
            pricing.list_paise(base_product_total_paise),
            list_weights,
        ),
        line_product_subtotals_paise,
    )
    line_discounts_paise = [
        listed - charged
        for listed, charged in zip(
            line_list_subtotals_paise,
            line_product_subtotals_paise,
            strict=True,
        )
    ]
    product_list_subtotal_paise = sum(line_list_subtotals_paise)
    discount_paise = sum(line_discounts_paise)

    now = datetime.now(timezone.utc)
    line_earliest_delivery_dates = [
        earliest_delivery_date(
            line.product.vendor,
            now=now,
            product_lead_time_days=line.product.dispatch_lead_time_days,
        )
        for line in priced_lines
    ]

    return ProductOrderQuote(
        priced_lines=priced_lines,
        vendor=vendor,
        base_product_total_paise=base_product_total_paise,
        raw_product_total_paise=raw_product_total_paise,
        product_subtotal_paise=product_subtotal_paise,
        product_list_subtotal_paise=product_list_subtotal_paise,
        discount_paise=discount_paise,
        shipping_paise=shipping_paise,
        total_paise=product_subtotal_paise + shipping_paise,
        earliest_delivery_date=max(line_earliest_delivery_dates),
        line_earliest_delivery_dates=line_earliest_delivery_dates,
        line_product_subtotals_paise=line_product_subtotals_paise,
        line_list_subtotals_paise=line_list_subtotals_paise,
        line_discounts_paise=line_discounts_paise,
    )


def serialize_product_order_quote(quote: ProductOrderQuote) -> dict:
    vendor = quote.vendor
    if quote.shipping_paise == 0:
        shipping_charge_type = "free"
    elif getattr(vendor, "shipping_flat_fee_paise", None) is not None:
        shipping_charge_type = "vendor_flat"
    else:
        shipping_charge_type = "vendor_product_rule"

    return {
        "currency": "INR",
        "vendor": {"id": vendor.id, "name": vendor.name},
        "items": [
            {
                "line_index": index,
                "product_id": line.product.id,
                "product_name": line.product.name,
                "image_url": line.product.image_url,
                "vendor_id": vendor.id,
                "vendor_name": vendor.name,
                "variant_id": line.variant.id if line.variant else None,
                "variant_name": line.variant.name if line.variant else None,
                "selected_addons": line.selected_addons,
                "customizations": line.customizations,
                "qty": line.qty,
                "line_list_subtotal_paise": quote.line_list_subtotals_paise[index],
                "line_discount_paise": quote.line_discounts_paise[index],
                "line_subtotal_paise": quote.line_product_subtotals_paise[index],
                "earliest_delivery_date": quote.line_earliest_delivery_dates[index].isoformat(),
            }
            for index, line in enumerate(quote.priced_lines)
        ],
        "shipping": {
            "vendor_id": vendor.id,
            "vendor_name": vendor.name,
            "fee_paise": quote.shipping_paise,
            "charge_type": shipping_charge_type,
            "applies_to_product_ids": [line.product.id for line in quote.priced_lines],
            "earliest_delivery_date": quote.earliest_delivery_date.isoformat(),
        },
        "breakdown": {
            "product_list_subtotal_paise": quote.product_list_subtotal_paise,
            "discount_paise": quote.discount_paise,
            "product_subtotal_paise": quote.product_subtotal_paise,
            "shipping_paise": quote.shipping_paise,
            "total_paise": quote.total_paise,
            "taxes_included": True,
        },
        "earliest_delivery_date": quote.earliest_delivery_date.isoformat(),
    }


@router.post("/product/quote")
async def quote_product_order(
    payload: ProductOrderQuotePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    ip = _client_ip(request)
    if not await _quote_rate_limiter.allow(ip):
        logger.warning("product_quote_rate_limited ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many requests. Try again in a moment.")

    quote = await build_product_order_quote(db, payload.items)
    return serialize_product_order_quote(quote)


@router.post("/product")
async def create_product_order(
    payload: ProductOrderPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    quote = await build_product_order_quote(db, payload.items)
    priced_lines = quote.priced_lines
    vendor = quote.vendor
    vendor_id = vendor.id
    product_total_paise = quote.raw_product_total_paise
    shipping_total_paise = quote.shipping_paise
    total_paise = quote.total_paise

    # Shipping is a pass-through to the vendor, not commissionable — the 90/10
    # split applies only to the product/customization portion, using the normal
    # vendor discounted amount (not the flash charge). The vendor gets 90% of
    # that amount plus shipping; the platform keeps the remainder. Defining the
    # platform amount this way keeps vendor + platform == total_paise exactly.
    vendor_amount_paise = round(product_total_paise * 0.90) + shipping_total_paise
    platform_amount_paise = total_paise - vendor_amount_paise

    if payload.delivery_date < quote.earliest_delivery_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "earliest available delivery date is "
                f"{quote.earliest_delivery_date.isoformat()}"
            ),
        )

    # Serviceability backstop: refuse orders for local-only products that don't
    # deliver to the shipping destination. The storefront should gate this
    # earlier, but this guard means we never accept an unfulfillable order.
    vendor_city_ids = (
        await db.execute(select(VendorCity.city_id).where(VendorCity.vendor_id == vendor_id))
    ).scalars().all()
    for product in {line.product.id: line.product for line in priced_lines}.values():
        # Match on pincode only, NOT the free-text city: city names are
        # duplicable and user-typed, so "Pune" + a Telangana pincode must not
        # pass on the name alone. Pincode prefixes are the authoritative signal
        # (same as the storefront pincode-check, which passes dest_city=None).
        result = await check_product_serviceability(
            db,
            ships_nationally=product.ships_nationally,
            vendor_city_ids=list(vendor_city_ids),
            dest_city=None,
            dest_pincode=payload.shipping_address.pincode,
        )
        if not result.serviceable:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{product.name}' does not deliver to "
                    f"{payload.shipping_address.city} ({payload.shipping_address.pincode})."
                ),
            )

    user = await get_or_create_customer(db, payload.customer)
    order = Order(
        id=generate_id("ord"),
        user_id=user.id,
        total_amount=Decimal(total_paise) / Decimal(100),
        currency="INR",
        status=OrderStatus.PENDING,
        order_type="product",
        vendor_id=vendor_id,
        vendor_amount_paise=vendor_amount_paise,
        platform_amount_paise=platform_amount_paise,
        shipping_name=payload.shipping_address.name,
        shipping_phone=payload.shipping_address.phone,
        shipping_line1=payload.shipping_address.line1,
        shipping_line2=payload.shipping_address.line2,
        shipping_city=payload.shipping_address.city,
        shipping_state=payload.shipping_address.state,
        shipping_pincode=payload.shipping_address.pincode,
        delivery_date=payload.delivery_date,
        gift_message=payload.gift_message,
        is_gift=payload.is_gift,
        hide_prices=payload.hide_prices,
        marketing_attribution=(
            payload.attribution.model_dump(exclude_none=True)
            if payload.attribution
            else None
        ),
    )
    db.add(order)
    await db.flush()

    # Persist the whole order-level shipping on one representative line (the
    # highest-fee item) and 0 on the rest, so summing line shipping_fee_paise
    # reproduces the actual charge instead of double-counting.
    shipping_line_index = max(
        range(len(priced_lines)),
        key=lambda i: priced_lines[i].shipping_fee_paise,
        default=-1,
    )
    for index, line in enumerate(priced_lines):
        db.add(
            OrderLineItem(
                id=generate_id("oli"),
                order_id=order.id,
                vendor_id=line.product.vendor_id,
                product_id=line.product.id,
                variant_id=line.variant.id if line.variant else None,
                product_name=line.product.name,
                product_image_url=line.product.image_url,
                selected_addons=line.selected_addons,
                customizations=line.customizations,
                qty=line.qty,
                unit_price_paise=line.unit_price_paise,
                line_total_paise=line.line_total_paise,
                shipping_fee_paise=(
                    shipping_total_paise if index == shipping_line_index else 0
                ),
            )
        )

    # TODO(route): when Razorpay Route is enabled, attach transfer instructions
    # for vendor_amount_paise to the vendor's linked account. For now we create
    # a normal Razorpay Order and keep the 90/10 split persisted on Order.
    razorpay_order = await razorpay_service.create_order(
        amount=total_paise,
        receipt=order.id,
        notes={
            "order_id": order.id,
            "order_type": "product",
            "vendor_id": vendor_id,
            "vendor_amount_paise": str(vendor_amount_paise),
            "platform_amount_paise": str(platform_amount_paise),
        },
    )
    order.razorpay_order_id = razorpay_order.get("id")
    await db.commit()

    return {
        "order_id": order.id,
        "razorpay_order_id": order.razorpay_order_id,
        "amount_paise": total_paise,
        "currency": "INR",
        "vendor_amount_paise": vendor_amount_paise,
        "platform_amount_paise": platform_amount_paise,
        "route_transfers": "stubbed",
        "quote": serialize_product_order_quote(quote),
    }


@router.get("/product/{order_id}")
async def get_product_order(
    order_id: str,
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Public order-confirmation lookup.

    Authorised by a separate high-entropy confirmation_token (see the Order
    model), NOT the order id alone — the id is only a timestamp plus 32
    random bits, and an order's rough creation time is knowable, so it isn't
    enough entropy to gate phone/address/gift-message/tracking behind. The
    token is handed to the buyer once at checkout and must be supplied here.
    """
    order = (
        await db.execute(
            select(Order)
            .where(Order.id == order_id, Order.order_type == "product")
            .options(selectinload(Order.line_items))
        )
    ).scalar_one_or_none()
    if order is None or not secrets.compare_digest(order.confirmation_token, token):
        raise HTTPException(status_code=404, detail="order not found")

    return {
        "order_id": order.id,
        "status": order.status.value,
        "fulfillment_status": order.fulfillment_status,
        "currency": order.currency,
        "total_amount": str(order.total_amount),
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "gift_message": order.gift_message,
        "hide_prices": order.hide_prices,
        "tracking_number": order.tracking_number,
        "tracking_carrier": order.tracking_carrier,
        "tracking_url": order.tracking_url,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "shipping_address": {
            "name": order.shipping_name,
            "phone": order.shipping_phone,
            "line1": order.shipping_line1,
            "line2": order.shipping_line2,
            "city": order.shipping_city,
            "state": order.shipping_state,
            "pincode": order.shipping_pincode,
        },
        "items": [
            {
                "product_id": line.product_id,
                "product_name": line.product_name,
                "image_url": line.product_image_url,
                "variant_id": line.variant_id,
                "selected_addons": line.selected_addons,
                "customizations": line.customizations,
                "qty": line.qty,
                "unit_price_paise": line.unit_price_paise,
                "line_total_paise": line.line_total_paise,
                "shipping_fee_paise": line.shipping_fee_paise,
            }
            for line in order.line_items
        ],
    }


@router.post("/product/verify")
async def verify_product_order_payment(
    payload: VerifyPaymentPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify a Razorpay Checkout success signature before showing the UI confirmation.

    This is a UX gate only — the ``payment.captured`` webhook remains the sole
    authority that marks an Order PAID and triggers vendor fulfilment. This
    endpoint never writes order status; it just confirms the signature is
    genuine and reports the order's current (webhook-driven) status so the
    client doesn't show "success" off an unverified callback.
    """
    if not razorpay_service.verify_checkout_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="invalid payment signature")

    order = (
        await db.execute(select(Order).where(Order.razorpay_order_id == payload.razorpay_order_id))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    return {
        "verified": True,
        "order_id": order.id,
        "status": order.status.value,
        "confirmation_token": order.confirmation_token,
    }
