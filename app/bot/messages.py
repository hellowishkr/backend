"""Outbound message templates + chat-history persistence helpers.

Two distinct concerns live here for historical reasons:

1. **Reminder body templates** — ``REMINDER``, ``REMINDER_TODAY``,
   ``FRIEND_REMINDER``, ``FRIEND_REMINDER_TODAY``. These are the strings
   the reminder cron (``app/jobs/send_reminders.py``) formats and sends via
   WhatsApp when a Meta-approved template is unavailable or falls back.
   They are NOT Meta-Business-template names — those live in
   ``app/bot/whatsapp_templates.py`` as `TEMPLATE_*` constants
   (e.g. `TEMPLATE_FRIEND_OCCASION_REMINDER = "occasion_reminder_notification"`)
   and are referenced by string in the dispatch code.

2. ``append_message`` — a thin helper that persists a ConversationMessage
   row for each WhatsApp turn. Separate from ADK's session store.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationMessage, MessageRole


# ─────────────────────────────────────────────────────────
# Reminder body templates (used as the send_text fallback when a Meta
# pre-approved template is unavailable). All support ``.format()`` with
# the named kwargs listed below.
# ─────────────────────────────────────────────────────────

# User-stream: the USER is being reminded about an occasion.
# kwargs: subject, days, date, suggestions
REMINDER = (
    "🎁 Reminder: {subject} is in *{days} days* ({date}).\n\n"
    "Need gift ideas? Try:\n{suggestions}\n\n"
    "Reply *suggest* for more ideas, or *menu* for options."
)

# User-stream, day-of. kwargs: subject
REMINDER_TODAY = (
    "🎉 Today is {subject}!\n\n"
    "Want gift ideas? Reply *suggest*.\n\n"
    "For now, you can buy from your preferred store."
)

# Friend-stream: a friend is being reminded about the CELEBRANT's occasion.
# kwargs: celebrant_name, occasion, days, date, preferences, budget
FRIEND_REMINDER = (
    "🎁 Heads up from Wishkr — {celebrant_name}'s {occasion} is in "
    "*{days} days* ({date}).\n\n"
    "They'd love something from:\n{preferences}\n\n"
    "Budget guide: {budget}\n\n"
    "Want gift ideas? Reply *suggest*.\n\n"
    "For now, you can buy from your preferred store."
)

# Friend-stream, day-of. kwargs: celebrant_name, occasion, preferences, budget
FRIEND_REMINDER_TODAY = (
    "🎉 Today is {celebrant_name}'s {occasion}!\n\n"
    "A last-minute gift goes a long way. They'd love:\n{preferences}\n\n"
    "Budget guide: {budget}\n\n"
    "Want gift ideas? Reply *suggest*.\n\n"
    "For now, you can buy from your preferred store."
)


# ─────────────────────────────────────────────────────────
# Payment / order lifecycle messages (sent to users via send_text)
# ─────────────────────────────────────────────────────────

# kwargs: order_id
PAYMENT_LINK_EXPIRED = (
    "⏰ Your payment link for order *{order_id}* has expired.\n\n"
    "If you still want to send the gift, just reply *send gift* and "
    "I'll create a fresh link for you."
)

# kwargs: order_id
PAYMENT_LINK_CANCELLED = (
    "❌ Your payment link for order *{order_id}* has been cancelled.\n\n"
    "If this was a mistake or you'd like to try again, reply *send gift* "
    "and I'll set up a new one."
)

# kwargs: brand, amount, code, pin, expiry
VOUCHER_DELIVERED = (
    "🎁 Your *{brand}* gift card for ₹{amount} is ready!\n\n"
    "Code: *{code}*\nPIN: *{pin}*\nValid until: {expiry}\n\n"
    "Enjoy! 🎉"
)

# No kwargs — generic failure message
VOUCHER_FAILED = (
    "😕 We ran into an issue processing your gift card. "
    "Our team has been notified and will sort this out for you shortly. "
    "You will not be charged if the issue isn't resolved."
)

# kwargs: order_id, amount
REFUND_INITIATED = (
    "↩️ A refund of ₹{amount} for order *{order_id}* has been initiated.\n\n"
    "It typically takes 5-7 business days to reach your account depending "
    "on your bank."
)

# kwargs: order_id, amount
REFUND_PROCESSED = (
    "✅ Your refund of ₹{amount} for order *{order_id}* has been processed "
    "and is on its way to your account.\n\n"
    "If you don't see it in 2-3 business days, please contact your bank."
)

# kwargs: order_id
DISPUTE_ACKNOWLEDGED = (
    "📋 We've received a dispute for order *{order_id}*.\n\n"
    "Our team is reviewing it and will be in touch with you shortly. "
    "Please don't worry — we'll make sure this is resolved fairly."
)

# kwargs: sender_name, brand, amount, message, code, pin, expiry
GIFT_RECEIVED = (
    "🎁 *{sender_name}* sent you a gift!\n\n"
    "*{brand}* gift card · ₹{amount}\n\n"
    "Their message: _{message}_\n\n"
    "Code: *{code}*\nPIN: *{pin}*\nValid until: {expiry}"
)

# kwargs: sender_name, brand, amount, code, pin, expiry
GIFT_RECEIVED_NO_MESSAGE = (
    "🎁 *{sender_name}* sent you a gift!\n\n"
    "*{brand}* gift card · ₹{amount}\n\n"
    "Code: *{code}*\nPIN: *{pin}*\nValid until: {expiry}"
)

# No kwargs — sent to celebrant after gift delivery to prompt them to use Wishkr
VIRAL_AFTER_GIFT_RECEIVED = (
    "💝 Loved getting a gift on WhatsApp? You can send one too!\n\n"
    "Just say *send gift* and I'll help you pick something for someone special."
)

# No kwargs — sent to buyer after gift is delivered to prompt referrals
VIRAL_AFTER_GIFT_SENT = (
    "✅ Your gift was just delivered!\n\n"
    "Know someone else with a birthday or occasion coming up? "
    "Reply *send gift* and I'll sort it in under a minute. 🎁"
)

# kwargs: celebrant_name, brand, amount, message_preview, delivery_date
FRIEND_PAYMENT_CONFIRMED = (
    "✅ Payment confirmed! Your *{brand}* gift card worth ₹{amount} for "
    "*{celebrant_name}* is locked in.\n\n"
    "Message: {message_preview}\n"
    "Delivery date: *{delivery_date}*\n\n"
    "We'll send it to them on the day. 🎉"
)


# ─────────────────────────────────────────────────────────
# Founder alert templates (sent via send_founder_alert)
# ─────────────────────────────────────────────────────────

# kwargs: order_id, phone, amount, brand, error
ALERT_FAILED_ORDER = (
    "🚨 ORDER FAILED\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount: ₹{amount} | Brand: {brand}\n"
    "Error: {error}"
)

# kwargs: count, phones
ALERT_PAYMENT_FAILURE_BURST = (
    "⚠️ PAYMENT FAILURE BURST\n"
    "{count} payment failures in the last 10 minutes.\n"
    "Phones involved: {phones}"
)

# kwargs: order_id, phone, amount, reason, respond_by
ALERT_DISPUTE_CREATED = (
    "🚨 DISPUTE RAISED\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount: ₹{amount}\n"
    "Reason: {reason}\n"
    "Respond by: {respond_by}"
)

# kwargs: order_id, phone, amount
ALERT_DISPUTE_WON = (
    "✅ DISPUTE WON\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount retained: ₹{amount}"
)

# kwargs: order_id, phone, amount
ALERT_DISPUTE_LOST = (
    "❌ DISPUTE LOST\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount lost: ₹{amount}\n"
    "Razorpay will deduct this from the next settlement."
)

# kwargs: order_id, phone, amount
ALERT_REFUND_PROCESSED = (
    "↩️ REFUND PROCESSED\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount refunded: ₹{amount}"
)

# kwargs: order_id, phone, amount, reason
ALERT_REFUND_FAILED = (
    "🚨 REFUND FAILED\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Amount: ₹{amount}\n"
    "Reason: {reason}\n"
    "Manual refund required via Razorpay dashboard."
)

# kwargs: order_id, phone, expected, received
ALERT_PARTIAL_PAYMENT = (
    "⚠️ PARTIAL PAYMENT\n"
    "Order: {order_id} | Phone: {phone}\n"
    "Expected: ₹{expected} | Received: ₹{received}\n"
    "Voucher NOT issued. Manual review required."
)

# kwargs: method, severity, began_at, instrument_line
ALERT_PAYMENT_DOWNTIME_STARTED = (
    "⚠️ RAZORPAY DOWNTIME\n"
    "Method: {method} | Severity: {severity}\n"
    "{instrument_line}"
    "Started: {began_at}"
)

# kwargs: method, began_at, resolved_at
ALERT_PAYMENT_DOWNTIME_RESOLVED = (
    "✅ RAZORPAY DOWNTIME RESOLVED\n"
    "Method: {method}\n"
    "Started: {began_at} | Resolved: {resolved_at}"
)


async def append_message(
    db: AsyncSession,
    phone: str,
    role: MessageRole,
    content: str | None = None,
) -> None:
    db.add(ConversationMessage(phone=phone, role=role, content=content))
    await db.flush()
