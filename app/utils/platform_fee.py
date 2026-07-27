"""Platform fee calculation for gift card orders.

Fee structure (configurable via settings):
  - platform_fee     = face_value × platform_fee_rate          (default 2%)
  - platform_fee_gst = platform_fee × platform_fee_gst_rate    (default 18%)
  - total_charged    = face_value + platform_fee + platform_fee_gst

Both fee components are rounded to 2 decimal places (half-up) so that
face_value + fee + gst always equals total_charged exactly — no paise drift.
"""

from decimal import ROUND_HALF_UP, Decimal

from app.core.config import settings


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_platform_fee(face_value: float | Decimal) -> dict:
    """Return fee breakdown for a given gift card face value.

    Returns:
        gift_amount:      Decimal — face value (what goes to Hubble)
        platform_fee:     Decimal — gateway fee charged to user
        platform_fee_gst: Decimal — GST on the gateway fee
        total_amount:     Decimal — what Razorpay charges the user
        fee_label:        str     — human-readable label for the bot/UI
    """
    face = _round(Decimal(str(face_value)))
    fee = _round(face * Decimal(str(settings.platform_fee_rate)))
    gst = _round(fee * Decimal(str(settings.platform_fee_gst_rate)))
    total = face + fee + gst

    gst_pct = int(settings.platform_fee_gst_rate * 100)
    fee_pct = int(settings.platform_fee_rate * 100)
    fee_label = f"Payment gateway fee ({fee_pct}%) + GST ({gst_pct}%): ₹{fee + gst}"

    fee_line = f"Gateway fee ({fee_pct}%) + GST"
    payment_slip = (
        "```\n"
        "Order Summary\n"
        "─────────────────────────\n"
        f"Gift Card          ₹{face:>8.2f}\n"
        f"{fee_line:<22} ₹{fee + gst:>6.2f}\n"
        "─────────────────────────\n"
        f"Total              ₹{total:>8.2f}\n"
        "```"
    )

    return {
        "gift_amount": face,
        "platform_fee": fee,
        "platform_fee_gst": gst,
        "total_amount": total,
        "fee_label": fee_label,
        "payment_slip": payment_slip,
    }
