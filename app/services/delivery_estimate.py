"""Earliest feasible delivery date for marketplace product orders.

Vendor-keyed, not gift-type-keyed — product orders are single-vendor
(enforced in create_product_order), so one lead time applies to the whole
order. This is deliberately separate from fulfillment_policy.py, which is
keyed on GiftType/Purchase for the voucher flow and isn't a fit here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.vendor import Vendor

# Conservative fallback when a vendor hasn't configured a lead time yet.
DEFAULT_LEAD_TIME_HOURS = 5 * 24

# Safety margin for multi-day fulfilment. Verified sub-day services use their
# same-day cutoff instead, otherwise "same day" could never reach checkout.
SAFETY_BUFFER_DAYS = 1
INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")


def effective_delivery_lead_time_hours(
    vendor: Vendor, *, product_lead_time_days: int | None = None
) -> int:
    """Return the most specific configured lead time for a product."""
    if product_lead_time_days is not None:
        return product_lead_time_days * 24
    if vendor.delivery_lead_time_hours is not None:
        return vendor.delivery_lead_time_hours
    return DEFAULT_LEAD_TIME_HOURS


def supports_express_delivery(vendor: Vendor, *, product_lead_time_days: int | None = None) -> bool:
    """Whether this vendor/product combination has a verified same-day SLA."""
    return (
        effective_delivery_lead_time_hours(vendor, product_lead_time_days=product_lead_time_days)
        < 24
        and vendor.same_day_cutoff_hour is not None
    )


def express_delivery_available_today(
    vendor: Vendor,
    *,
    now: datetime | None = None,
    product_lead_time_days: int | None = None,
) -> bool:
    """Whether an express-capable product can still be ordered for today."""
    if not supports_express_delivery(vendor, product_lead_time_days=product_lead_time_days):
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(INDIA_TIMEZONE).hour < int(vendor.same_day_cutoff_hour)


def earliest_delivery_date(
    vendor: Vendor, *, now: datetime | None = None, product_lead_time_days: int | None = None
) -> date:
    """Earliest calendar date the vendor could plausibly deliver by.

    A configured sub-day service remains eligible for today before the
    vendor's same-day cutoff. Cutoff hours are India-local business hours, not
    UTC hours. Multi-day deliveries retain the one-day safety buffer.

    ``product_lead_time_days`` is the vendor's own per-product dispatch lead
    time (Product.dispatch_lead_time_days). When set, it takes priority over
    the vendor-wide default — it's a more specific claim the vendor made about
    this exact product, whereas the vendor-level field is a generic fallback
    most vendors never configure. For a multi-product order, pass the max
    (slowest) lead time across the cart, since the order ships together.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_current = current.astimezone(INDIA_TIMEZONE)
    lead_hours = effective_delivery_lead_time_hours(
        vendor, product_lead_time_days=product_lead_time_days
    )

    if lead_hours < 24 and vendor.same_day_cutoff_hour is not None:
        if local_current.hour < vendor.same_day_cutoff_hour:
            return local_current.date()
        return (local_current + timedelta(days=1)).date()

    base = (local_current + timedelta(hours=lead_hours)).date()
    return base + timedelta(days=SAFETY_BUFFER_DAYS)
