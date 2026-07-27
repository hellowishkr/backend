"""Provider-neutral fulfillment policy helpers.

The purchase table is the user-facing source of truth. Providers are an
implementation detail: Hubble gift cards, cakes, flowers, private theatres,
and future local vendors all stamp the same policy fields so schedulers know
when to place/dispatch the provider order.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone

from app.models import GiftType, Purchase


class FulfillmentPolicy(str, enum.Enum):
    IMMEDIATE_AFTER_PAYMENT = "IMMEDIATE_AFTER_PAYMENT"
    ON_DELIVERY_DATE = "ON_DELIVERY_DATE"
    HOURS_BEFORE = "HOURS_BEFORE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


DEFAULT_PROVIDER_BY_GIFT_TYPE: dict[GiftType, str] = {
    GiftType.GIFT_CARD: "hubble",
    GiftType.FLOWERS: "local_vendor",
    GiftType.CAKE: "local_vendor",
    GiftType.CHOCOLATES: "local_vendor",
    GiftType.EXPERIENCE: "local_vendor",
    GiftType.PRIVATE_THEATRE: "local_vendor",
    GiftType.PERSONALIZED: "local_vendor",
    GiftType.DECORATION: "local_vendor",
    GiftType.OTHER: "manual",
}

# Lead-time defaults are deliberately conservative. These can become
# merchant-specific later, but category defaults give us safe scheduling now.
DEFAULT_LEAD_HOURS_BY_GIFT_TYPE: dict[GiftType, int] = {
    GiftType.FLOWERS: 12,
    GiftType.CAKE: 24,
    GiftType.CHOCOLATES: 24,
    GiftType.PERSONALIZED: 72,
    GiftType.DECORATION: 24,
}

IMMEDIATE_GIFT_TYPES = {
    GiftType.PRIVATE_THEATRE,
    GiftType.EXPERIENCE,
}


def fulfillment_policy_for(
    gift_type: GiftType,
    *,
    delivery_scheduled_at: datetime | None = None,
) -> tuple[FulfillmentPolicy, int | None]:
    """Return the default scheduling policy for a gift category."""
    if gift_type == GiftType.GIFT_CARD:
        if delivery_scheduled_at is not None:
            return FulfillmentPolicy.ON_DELIVERY_DATE, None
        return FulfillmentPolicy.IMMEDIATE_AFTER_PAYMENT, None

    if gift_type in IMMEDIATE_GIFT_TYPES:
        return FulfillmentPolicy.IMMEDIATE_AFTER_PAYMENT, None

    lead_hours = DEFAULT_LEAD_HOURS_BY_GIFT_TYPE.get(gift_type)
    if lead_hours is not None:
        return FulfillmentPolicy.HOURS_BEFORE, lead_hours

    return FulfillmentPolicy.MANUAL_REVIEW, None


def compute_fulfillment_due_at(
    *,
    policy: FulfillmentPolicy | str | None,
    delivery_scheduled_at: datetime | None,
    lead_time_hours: int | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Compute when we should place/fulfill the provider order."""
    current = now or datetime.now(timezone.utc)
    try:
        policy_value = FulfillmentPolicy(policy) if policy else None
    except ValueError:
        return None

    if policy_value == FulfillmentPolicy.IMMEDIATE_AFTER_PAYMENT:
        return current
    if policy_value == FulfillmentPolicy.ON_DELIVERY_DATE:
        return delivery_scheduled_at or current
    if policy_value == FulfillmentPolicy.HOURS_BEFORE:
        if delivery_scheduled_at is None:
            return current
        due = delivery_scheduled_at - timedelta(hours=int(lead_time_hours or 0))
        return max(due, current)
    return None


def apply_fulfillment_defaults(
    purchase: Purchase,
    *,
    provider_product_id: str | None = None,
    recipient_user_id: str | None = None,
    recipient_phone: str | None = None,
    now: datetime | None = None,
) -> Purchase:
    """Stamp provider-neutral defaults onto a purchase before flushing."""
    purchase.provider = purchase.provider or DEFAULT_PROVIDER_BY_GIFT_TYPE.get(
        purchase.gift_type, "manual"
    )
    if provider_product_id and not purchase.provider_product_id:
        purchase.provider_product_id = provider_product_id
    if recipient_user_id and not purchase.recipient_user_id:
        purchase.recipient_user_id = recipient_user_id
    if recipient_phone and not purchase.recipient_phone:
        purchase.recipient_phone = recipient_phone

    policy, lead_hours = fulfillment_policy_for(
        purchase.gift_type,
        delivery_scheduled_at=purchase.delivery_scheduled_at,
    )
    purchase.fulfillment_policy = purchase.fulfillment_policy or policy.value
    if purchase.fulfillment_lead_time_hours is None and lead_hours is not None:
        purchase.fulfillment_lead_time_hours = lead_hours
    purchase.fulfillment_due_at = purchase.fulfillment_due_at or compute_fulfillment_due_at(
        policy=purchase.fulfillment_policy,
        delivery_scheduled_at=purchase.delivery_scheduled_at,
        lead_time_hours=purchase.fulfillment_lead_time_hours,
        now=now,
    )
    return purchase


def is_hubble_gift_card(purchase: Purchase) -> bool:
    """True for legacy and new Hubble-backed gift-card purchases."""
    return (
        purchase.gift_type == GiftType.GIFT_CARD
        and (purchase.provider in (None, "hubble"))
    )
