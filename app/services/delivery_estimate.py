"""Earliest feasible delivery date for marketplace product orders.

Vendor-keyed, not gift-type-keyed — product orders are single-vendor
(enforced in create_product_order), so one lead time applies to the whole
order. This is deliberately separate from fulfillment_policy.py, which is
keyed on GiftType/Purchase for the voucher flow and isn't a fit here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.vendor import Vendor

# Conservative fallback when a vendor hasn't configured a lead time yet.
DEFAULT_LEAD_TIME_HOURS = 5 * 24

# Safety margin added on top of the vendor's lead time, so the earliest date we
# promise always has a day of slack against missed dispatch.
SAFETY_BUFFER_DAYS = 1


def earliest_delivery_date(
    vendor: Vendor, *, now: datetime | None = None, product_lead_time_days: int | None = None
) -> date:
    """Earliest calendar date the vendor could plausibly deliver by.

    A one-day safety buffer is always applied. Same-day eligibility only
    applies when the lead time is zero and the current time is before the
    vendor's same-day cutoff hour.

    ``product_lead_time_days`` is the vendor's own per-product dispatch lead
    time (Product.dispatch_lead_time_days). When set, it takes priority over
    the vendor-wide default — it's a more specific claim the vendor made about
    this exact product, whereas the vendor-level field is a generic fallback
    most vendors never configure. For a multi-product order, pass the max
    (slowest) lead time across the cart, since the order ships together.
    """
    current = now or datetime.now(timezone.utc)
    if product_lead_time_days is not None:
        lead_hours = product_lead_time_days * 24
    elif vendor.delivery_lead_time_hours is not None:
        lead_hours = vendor.delivery_lead_time_hours
    else:
        lead_hours = DEFAULT_LEAD_TIME_HOURS

    if lead_hours == 0 and vendor.same_day_cutoff_hour is not None:
        if current.hour < vendor.same_day_cutoff_hour:
            base = current.date()
        else:
            base = (current + timedelta(days=1)).date()
    else:
        base = (current + timedelta(hours=lead_hours)).date()

    return base + timedelta(days=SAFETY_BUFFER_DAYS)
