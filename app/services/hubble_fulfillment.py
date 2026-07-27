"""Hubble voucher issuance helpers.

Money flow policy:
- Immediate/self gifts: issue the Hubble voucher after Razorpay payment.
- Scheduled friend gifts: collect payment now, issue the Hubble voucher on
  the delivery date, then send it to the celebrant.
"""

import re
from datetime import datetime, timezone

from app.core.config import settings
from app.models import Purchase, PurchaseStatus
from app.services.hubble import hubble_service, HubbleOrderError, HubbleUnavailableError


def hubble_customer_details(reference_id: str) -> dict[str, str]:
    """Pseudonymised customer fields for Hubble API order creation.

    The Hubble/Gullak MSA flow agreed on Apr 30 / May 1 says Wishkr should
    pass only a Wishkr-side reference token to Hubble and keep real end-user
    identity mapping internally. Hubble's API still has customerDetails
    fields, so populate them with Wishkr-owned reference/support data, not
    buyer or recipient PII.
    """
    raw = settings.hubble_customer_phone
    if not raw:
        raise HubbleOrderError("HUBBLE_CUSTOMER_PHONE is required for Hubble order creation")

    # Strip country code (+91 / 91) — Hubble expects a bare 10-digit number
    phone = raw.lstrip("+")
    if phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]

    if not re.fullmatch(r"[6-9]\d{9}|4444", phone):
        raise HubbleOrderError(
            f"HUBBLE_CUSTOMER_PHONE '{raw}' is not a valid 10-digit Indian mobile number (6-9 start) or test value 4444"
        )

    return {
        "customer_name": f"Wishkr Order {reference_id}",
        "customer_phone": phone,
        "customer_email": "",
    }


def should_defer_hubble_issue(purchase: Purchase, now: datetime | None = None) -> bool:
    """True when a paid gift should be issued later by the delivery cron."""
    if purchase.delivery_scheduled_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    scheduled_at = purchase.delivery_scheduled_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return scheduled_at > now


async def _validate_denomination(product_id: str, amount: int) -> None:
    """Fail fast if amount isn't a valid denomination for this brand.

    Uses the in-memory brand cache (refreshed every 6h) to avoid a live API
    call on every order. Only raises for FIXED denomination brands — FLEXIBLE
    brands are valid for any amount within min/max.
    """
    try:
        brands = await hubble_service.get_brands()
    except Exception:
        return  # cache miss — let Hubble reject if invalid

    brand = next((b for b in brands if b.get("id") == product_id), None)
    if not brand:
        return  # unknown product — let Hubble handle it

    denom_type = brand.get("denominationType", "FLEXIBLE")
    if denom_type != "FIXED":
        return

    restrictions = brand.get("amountRestrictions", {})
    allowed = restrictions.get("denominations", [])
    if allowed and amount not in allowed:
        raise HubbleOrderError(
            f"Amount ₹{amount} is not a valid denomination for '{brand.get('title')}'. "
            f"Allowed: {sorted(allowed)}"
        )


async def issue_hubble_voucher(purchase: Purchase) -> dict:
    """Buy/issue a Hubble voucher and stamp the Purchase row in memory.

    Caller is responsible for adding/committing the purchase in its current
    transaction. Raises HubbleOrderError on non-usable Hubble responses.
    """
    if settings.mvp_reminder_only:
        raise HubbleOrderError("mvp_reminder_only")

    if not purchase.hubble_product_id:
        raise HubbleOrderError(f"purchase {purchase.id} has no hubble_product_id")

    await _validate_denomination(purchase.hubble_product_id, int(purchase.amount))

    try:
        hubble_result = await hubble_service.create_order(
            product_id=purchase.hubble_product_id,
            amount=int(purchase.amount),
            denomination=int(purchase.amount),
            quantity=1,
            reference_id=purchase.id,
            **hubble_customer_details(purchase.id),
        )
    except HubbleUnavailableError as e:
        raise HubbleOrderError(e.reason) from e
    voucher = hubble_result.get("vouchers", [{}])[0]
    card_number = voucher.get("cardNumber")
    if not card_number:
        raise HubbleOrderError(
            f"Hubble returned no cardNumber for purchase {purchase.id} "
            f"(hubble_order_id={hubble_result.get('id')})"
        )

    purchase.hubble_order_id = hubble_result.get("id")
    purchase.provider = purchase.provider or "hubble"
    purchase.provider_product_id = purchase.provider_product_id or purchase.hubble_product_id
    purchase.provider_order_id = purchase.provider_order_id or purchase.hubble_order_id
    purchase.voucher_code = card_number
    purchase.voucher_pin = voucher.get("cardPin")
    purchase.status = PurchaseStatus.VOUCHER_ISSUED

    valid_till = voucher.get("validTill")
    if valid_till:
        try:
            purchase.voucher_expiry = datetime.fromisoformat(valid_till).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    return hubble_result
