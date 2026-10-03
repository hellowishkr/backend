"""Customer-facing order email notifications.

Dispatches branded transactional emails for order lifecycle & fulfillment transitions:
- Shipped (with carrier, tracking number, and tracking URL)
- Delivered
- Cancelled
- Refund processed

Emails are best-effort: failures are logged and never raise to the caller or abort
a database transaction.
"""

from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.config import settings
from app.models.order import Order
from app.models.order_line_item import OrderLineItem
from app.models.user import User
from app.utils.email import send_email

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("wishkr.order_notifications")

# ─── Brand Colors (matching storefront palette) ─────────────────────────────
_EMAIL_CREAM = "#fbf8f3"
_EMAIL_CREAM2 = "#f3ece2"
_EMAIL_GOLD = "#d4a853"
_EMAIL_PURPLE = "#7b5b9a"
_EMAIL_INK = "#2a2233"
_EMAIL_MUTED = "#7a7186"
_EMAIL_LINE = "#e8e0d4"
_EMAIL_GREEN = "#16a34a"
_EMAIL_ROSE = "#e85d9c"
_WISHKR_LOGO_URL = f"{settings.wishkr_website_url.rstrip('/')}/images/logo.png"


def _esc(value: object | None) -> str:
    """HTML escape helper for email interpolation."""
    return html_lib.escape(str(value)) if value is not None else ""


def _email_shell(*, title: str, preheader: str, inner: str) -> str:
    """Wrap email body inside a centered, responsive card matching Wishkr styling."""
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{_EMAIL_CREAM2};">'
        f'<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</span>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_EMAIL_CREAM2};padding:24px 0;"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;background:{_EMAIL_CREAM};border-radius:16px;overflow:hidden;'
        f'border:1px solid {_EMAIL_LINE};font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        f'<tr><td style="background:#ffffff;padding:20px 28px;border-bottom:1px solid {_EMAIL_LINE};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td align="left" style="vertical-align:middle;">'
        f'<a href="{settings.wishkr_website_url}" target="_blank" style="text-decoration:none;display:inline-block;">'
        f'<img src="{_WISHKR_LOGO_URL}" alt="Wishkr" width="112" height="36" '
        f'style="display:block;border:0;outline:none;text-decoration:none;width:112px;height:auto;max-height:36px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;font-size:22px;font-weight:800;color:{_EMAIL_PURPLE};" />'
        f'</a>'
        f'</td>'
        f'<td align="right" style="vertical-align:middle;">'
        f'<span style="display:inline-block;padding:5px 12px;border-radius:20px;background:{_EMAIL_CREAM2};color:{_EMAIL_PURPLE};font-size:12px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;">{_esc(title)}</span>'
        f'</td>'
        f'</tr></table>'
        f'</td></tr>'
        f'<tr><td style="padding:28px;">{inner}</td></tr>'
        f'<tr><td style="padding:20px 28px;background:{_EMAIL_CREAM2};color:{_EMAIL_MUTED};font-size:12px;text-align:center;line-height:1.6;">'
        f'<div style="font-weight:600;color:{_EMAIL_PURPLE};font-size:13px;margin-bottom:4px;">Wish kar, Wishkr kar</div>'
        f'<div style="margin-bottom:4px;"><a href="{settings.wishkr_website_url}" target="_blank" style="color:{_EMAIL_PURPLE};text-decoration:none;">wishkr.com</a></div>'
        f'Reply to this email or message Wishkr if you have any questions.</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def _section_heading(text: str) -> str:
    return (
        f'<div style="font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;'
        f'color:{_EMAIL_PURPLE};margin:22px 0 8px;">{_esc(text)}</div>'
    )


def _shipping_address_summary(order: Order) -> str:
    parts = [
        order.shipping_name,
        order.shipping_line1,
        order.shipping_line2,
        order.shipping_city,
        order.shipping_state,
        order.shipping_pincode,
    ]
    return ", ".join(p for p in parts if p)


async def _get_customer_info(db: AsyncSession, order: Order) -> tuple[str, str, str]:
    user = await db.get(User, order.user_id) if order.user_id else None
    customer_name = (user.name if user else order.shipping_name) or "Customer"
    customer_phone = (user.phone if user else order.shipping_phone) or ""
    customer_email = (user.email if user else None) or ""
    return customer_name, customer_phone, customer_email


async def _load_line_items(db: AsyncSession, order_id: str) -> list[OrderLineItem]:
    result = await db.execute(
        select(OrderLineItem).where(OrderLineItem.order_id == order_id)
    )
    return list(result.scalars().all())


def _render_line_items_html(line_items: list[OrderLineItem]) -> str:
    if not line_items:
        return ""
    rows = []
    for li in line_items:
        img_tag = (
            f'<img src="{_esc(li.product_image_url)}" width="56" height="56" alt="" '
            f'style="width:56px;height:56px;border-radius:8px;object-fit:cover;border:1px solid {_EMAIL_LINE};display:block;">'
            if li.product_image_url
            else f'<div style="width:56px;height:56px;border-radius:8px;background:{_EMAIL_CREAM2};"></div>'
        )
        addon_parts = []
        for addon in li.selected_addons or []:
            name = addon.get("name")
            qty = addon.get("quantity", 1)
            if name:
                addon_parts.append(f"{_esc(name)} ×{_esc(qty)}")
        addon_html = (
            f'<div style="font-size:12px;color:{_EMAIL_MUTED};margin-top:2px;">'
            + ", ".join(addon_parts)
            + "</div>"
            if addon_parts
            else ""
        )
        rows.append(
            f'<tr>'
            f'<td style="padding:10px 0;border-bottom:1px solid {_EMAIL_LINE};width:56px;vertical-align:middle;">{img_tag}</td>'
            f'<td style="padding:10px 0 10px 12px;border-bottom:1px solid {_EMAIL_LINE};vertical-align:middle;">'
            f'<div style="font-size:14px;font-weight:700;color:{_EMAIL_INK};">{_esc(li.product_name)}</div>'
            f'{addon_html}</td>'
            f'<td style="padding:10px 0;border-bottom:1px solid {_EMAIL_LINE};text-align:right;'
            f'font-size:14px;font-weight:600;color:{_EMAIL_INK};vertical-align:middle;white-space:nowrap;">×{_esc(li.qty)}</td>'
            f'</tr>'
        )
    return (
        _section_heading("Items in this order")
        + f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{"".join(rows)}</table>'
    )


def _render_line_items_text(line_items: list[OrderLineItem]) -> str:
    if not line_items:
        return ""
    lines = ["── Items ──"]
    for li in line_items:
        addons = ", ".join(
            f"{a.get('name')} x{a.get('quantity', 1)}"
            for a in (li.selected_addons or [])
            if a.get("name")
        )
        suffix = f" ({addons})" if addons else ""
        lines.append(f"• {li.product_name} ×{li.qty}{suffix}")
    return "\n".join(lines)


# ─── Shipped Notification ───────────────────────────────────────────────────

async def send_order_shipped_email(order: Order, db: AsyncSession) -> bool:
    """Send shipment notification email with tracking info to the buyer. Best-effort."""
    try:
        customer_name, _, customer_email = await _get_customer_info(db, order)
        if not customer_email:
            logger.info("Order shipment email skipped: no customer email on order %s", order.id)
            return False

        line_items = await _load_line_items(db, order.id)
        carrier = order.tracking_carrier or "Standard Delivery"
        tracking_num = order.tracking_number or "—"
        tracking_url = order.tracking_url
        address_summary = _shipping_address_summary(order)
        web_track_url = (
            f"{settings.wishkr_website_url.rstrip('/')}/orders/track"
            f"?order_id={order.id}&email={customer_email}"
        )

        # Plain text body
        text_lines = [
            f"Hi {customer_name},",
            "",
            f"Great news! Your Wishkr order #{order.id} has been shipped and is on its way. 🚀",
            "",
            "── Shipment Details ──",
            f"Courier / Carrier: {carrier}",
            f"Tracking Number: {tracking_num}",
        ]
        if tracking_url:
            text_lines.append(f"Carrier Tracking: {tracking_url}")
        text_lines.append(f"Track on Wishkr: {web_track_url}")
        if address_summary:
            text_lines.append(f"Ship to: {address_summary}")
        if line_items:
            text_lines.extend(["", _render_line_items_text(line_items)])
        text_lines.extend([
            "",
            "We'll let you know once your gift is safely delivered.",
            "— Team Wishkr",
        ])

        # HTML body
        track_btn = (
            f'<div style="margin-top:16px;text-align:center;">'
            + (
                f'<a href="{_esc(tracking_url)}" target="_blank" style="display:inline-block;background:{_EMAIL_INK};'
                f'color:{_EMAIL_GOLD};padding:10px 22px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700;margin-right:8px;">'
                f'Track on Courier Site ↗</a>'
                if tracking_url else ""
            )
            + f'<a href="{_esc(web_track_url)}" target="_blank" style="display:inline-block;background:{_EMAIL_CREAM2};'
            f'color:{_EMAIL_PURPLE};padding:10px 22px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700;'
            f'border:1px solid {_EMAIL_LINE};">'
            f'View Live Timeline on Wishkr →</a>'
            f'</div>'
        )

        shipment_box = (
            f'<div style="background:#ffffff;border:1px solid {_EMAIL_LINE};border-radius:12px;padding:18px 20px;margin:18px 0;">'
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:{_EMAIL_PURPLE};margin-bottom:12px;">Shipment Details</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
            f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;width:125px;">Carrier</td>'
            f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">{_esc(carrier)}</td></tr>'
            f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;">Tracking Number</td>'
            f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;font-family:monospace;">{_esc(tracking_num)}</td></tr>'
            + (
                f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;">Destination</td>'
                f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">{_esc(address_summary)}</td></tr>'
                if address_summary else ""
            )
            + f'</table>'
            + track_btn
            + f'</div>'
        )

        inner = (
            f'<p style="margin:0 0 6px;font-size:16px;color:{_EMAIL_INK};">Hi {_esc(customer_name)},</p>'
            f'<p style="margin:0 0 16px;font-size:15px;color:{_EMAIL_INK};">'
            f'Great news! Your Wishkr order <strong>#{_esc(order.id)}</strong> has been shipped and is on its way. 🚀</p>'
            + shipment_box
            + _render_line_items_html(line_items)
            + f'<p style="margin:22px 0 0;font-size:14px;color:{_EMAIL_MUTED};">'
            f"We'll let you know as soon as your gift arrives. — Team Wishkr</p>"
        )

        html = _email_shell(
            title="Order shipped",
            preheader=f"Your Wishkr order #{order.id} has been shipped!",
            inner=inner,
        )

        return await send_email(
            to_email=customer_email,
            subject=f"Your Wishkr order #{order.id} has been shipped! 🚀",
            text="\n".join(text_lines),
            html=html,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send shipment email for order=%s: %s", order.id, exc)
        return False


# ─── Delivered Notification ─────────────────────────────────────────────────

async def send_order_delivered_email(order: Order, db: AsyncSession) -> bool:
    """Send delivery confirmation email to the buyer. Best-effort."""
    try:
        customer_name, _, customer_email = await _get_customer_info(db, order)
        if not customer_email:
            logger.info("Order delivered email skipped: no customer email on order %s", order.id)
            return False

        line_items = await _load_line_items(db, order.id)
        recipient = order.shipping_name or "the recipient"
        address_summary = _shipping_address_summary(order)

        text_lines = [
            f"Hi {customer_name},",
            "",
            f"Your Wishkr order #{order.id} has been delivered successfully to {recipient}! 🎁",
            "",
        ]
        if address_summary:
            text_lines.extend(["── Delivery Details ──", f"Delivered to: {address_summary}", ""])
        if line_items:
            text_lines.extend([_render_line_items_text(line_items), ""])
        text_lines.extend([
            "We hope your gift brought a huge smile! Thank you for choosing Wishkr.",
            "— Team Wishkr",
        ])

        delivery_box = (
            f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:18px 20px;margin:18px 0;">'
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:{_EMAIL_GREEN};margin-bottom:8px;">Delivered Successfully</div>'
            f'<div style="font-size:14px;color:{_EMAIL_INK};font-weight:600;">Delivered to: {_esc(recipient)}</div>'
            + (
                f'<div style="font-size:13px;color:{_EMAIL_MUTED};margin-top:4px;">{_esc(address_summary)}</div>'
                if address_summary else ""
            )
            + f'</div>'
        )

        inner = (
            f'<p style="margin:0 0 6px;font-size:16px;color:{_EMAIL_INK};">Hi {_esc(customer_name)},</p>'
            f'<p style="margin:0 0 16px;font-size:15px;color:{_EMAIL_INK};">'
            f'Your Wishkr order <strong>#{_esc(order.id)}</strong> has been delivered successfully! 🎁</p>'
            + delivery_box
            + _render_line_items_html(line_items)
            + f'<p style="margin:22px 0 0;font-size:14px;color:{_EMAIL_MUTED};">'
            f"We hope your gift brought a huge smile! Thank you for choosing Wishkr. — Team Wishkr</p>"
        )

        html = _email_shell(
            title="Order delivered",
            preheader=f"Your Wishkr order #{order.id} has been delivered!",
            inner=inner,
        )

        return await send_email(
            to_email=customer_email,
            subject=f"Your Wishkr order #{order.id} has been delivered! 🎁",
            text="\n".join(text_lines),
            html=html,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send delivery email for order=%s: %s", order.id, exc)
        return False


# ─── Cancelled Notification ─────────────────────────────────────────────────

async def send_order_cancelled_email(
    order: Order,
    db: AsyncSession,
    *,
    reason: str | None = None,
) -> bool:
    """Send order cancellation notification email to the buyer. Best-effort."""
    try:
        customer_name, _, customer_email = await _get_customer_info(db, order)
        if not customer_email:
            logger.info("Order cancellation email skipped: no customer email on order %s", order.id)
            return False

        line_items = await _load_line_items(db, order.id)
        has_payment = bool(order.amount_collected_paise and order.amount_collected_paise > 0)

        text_lines = [
            f"Hi {customer_name},",
            "",
            f"Your Wishkr order #{order.id} has been cancelled.",
        ]
        if reason:
            text_lines.append(f"Reason: {reason}")
        if has_payment:
            text_lines.append(
                "If payment was collected, a refund has been initiated to your original payment method in 5-7 business days."
            )
        if line_items:
            text_lines.extend(["", _render_line_items_text(line_items)])
        text_lines.extend([
            "",
            "If you have any questions or need further assistance, please reply directly to this email.",
            "— Team Wishkr",
        ])

        reason_html = ""
        if reason:
            reason_html = (
                f'<div style="background:#fff1f2;border:1px solid #fecdd3;border-radius:10px;'
                f'padding:12px 16px;color:#9f1239;font-size:13px;margin:16px 0;">'
                f'<strong>Reason:</strong> {_esc(reason)}</div>'
            )

        refund_note_html = ""
        if has_payment:
            refund_note_html = (
                f'<div style="background:#ffffff;border:1px solid {_EMAIL_LINE};border-radius:10px;'
                f'padding:14px 16px;font-size:13px;color:{_EMAIL_INK};margin:16px 0;">'
                f'💳 Any payment collected will be refunded to your original payment method within 5–7 business days.</div>'
            )

        inner = (
            f'<p style="margin:0 0 6px;font-size:16px;color:{_EMAIL_INK};">Hi {_esc(customer_name)},</p>'
            f'<p style="margin:0 0 14px;font-size:15px;color:{_EMAIL_INK};">'
            f'Your Wishkr order <strong>#{_esc(order.id)}</strong> has been cancelled.</p>'
            + reason_html
            + refund_note_html
            + _render_line_items_html(line_items)
            + f'<p style="margin:22px 0 0;font-size:14px;color:{_EMAIL_MUTED};">'
            f"If you have questions, please reply directly to this email. — Team Wishkr</p>"
        )

        html = _email_shell(
            title="Order cancelled",
            preheader=f"Your Wishkr order #{order.id} has been cancelled",
            inner=inner,
        )

        return await send_email(
            to_email=customer_email,
            subject=f"Your Wishkr order #{order.id} has been cancelled",
            text="\n".join(text_lines),
            html=html,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send cancellation email for order=%s: %s", order.id, exc)
        return False


# ─── Refunded Notification ──────────────────────────────────────────────────

async def send_order_refunded_email(
    order: Order,
    db: AsyncSession,
    *,
    amount_rupees: Decimal | float | None = None,
) -> bool:
    """Send refund confirmation email to the buyer. Best-effort."""
    try:
        customer_name, _, customer_email = await _get_customer_info(db, order)
        if not customer_email:
            logger.info("Order refund email skipped: no customer email on order %s", order.id)
            return False

        if amount_rupees is not None:
            refund_amount_str = f"{Decimal(str(amount_rupees)):.2f}"
        elif order.amount_collected_paise:
            refund_amount_str = f"{Decimal(order.amount_collected_paise) / Decimal(100):.2f}"
        else:
            refund_amount_str = f"{order.total_amount:.2f}" if order.total_amount else "0.00"

        text_lines = [
            f"Hi {customer_name},",
            "",
            f"We've processed a refund of ₹{refund_amount_str} for your Wishkr order #{order.id}.",
            "",
            "── Refund Details ──",
            f"Amount Refunded: ₹{refund_amount_str}",
            f"Order ID: #{order.id}",
            "Status: Processed",
            "",
            "It typically takes 5-7 business days for the funds to reflect in your original payment method (bank account, UPI, or card) depending on your bank's processing cycles.",
            "",
            "Thank you for your patience. If you have any questions, simply reply directly to this email.",
            "— Team Wishkr",
        ]

        refund_box = (
            f'<div style="background:#ffffff;border:1px solid {_EMAIL_LINE};border-radius:12px;padding:18px 20px;margin:18px 0;">'
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:{_EMAIL_PURPLE};margin-bottom:12px;">Refund Details</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
            f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;width:130px;">Amount Refunded</td>'
            f'<td style="color:{_EMAIL_INK};font-size:16px;font-weight:800;">₹{_esc(refund_amount_str)}</td></tr>'
            f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;">Order ID</td>'
            f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">#{_esc(order.id)}</td></tr>'
            f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;padding:4px 0;">Timeline</td>'
            f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">5–7 business days</td></tr>'
            f'</table>'
            f'</div>'
        )

        inner = (
            f'<p style="margin:0 0 6px;font-size:16px;color:{_EMAIL_INK};">Hi {_esc(customer_name)},</p>'
            f'<p style="margin:0 0 16px;font-size:15px;color:{_EMAIL_INK};">'
            f'We have processed a refund of <strong>₹{_esc(refund_amount_str)}</strong> for your Wishkr order <strong>#{_esc(order.id)}</strong>.</p>'
            + refund_box
            + f'<p style="margin:16px 0;font-size:13px;color:{_EMAIL_MUTED};line-height:1.5;">'
            f'The refunded amount will reflect back to your original payment method (bank account, UPI, or card) within 5–7 business days depending on your bank.</p>'
            f'<p style="margin:22px 0 0;font-size:14px;color:{_EMAIL_MUTED};">'
            f"If you don't see the funds after 7 business days, reply directly to this email with your order ID. — Team Wishkr</p>"
        )

        html = _email_shell(
            title="Refund processed",
            preheader=f"Refund processed for your Wishkr order #{order.id}",
            inner=inner,
        )

        return await send_email(
            to_email=customer_email,
            subject=f"Refund processed for your Wishkr order #{order.id}",
            text="\n".join(text_lines),
            html=html,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send refund email for order=%s: %s", order.id, exc)
        return False
