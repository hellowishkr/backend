"""Approved WhatsApp template names + per-occasion param formatters.

Single source of truth for:
  * Meta-approved template names (must match the WABA dashboard exactly).
  * How each ``OccasionType`` renders inside a friend-reminder phrase.

When a new occasion type is added to ``OccasionType``, the only place that
needs an entry is ``OCCASION_TEMPLATE_CONFIG`` below — dispatchers
(``send_reminders.py``, ``deliver_gifts.py``) consume the helpers and never
branch on the type themselves.

Why a separate phrasing from ``app.models.occasion.occasion_phrase``: that
helper renders "your birthday" / "Diwali" for the *self* voucher template
(``voucher_delivered``). Friend reminders are third-person, so they need
"Aarav's Birthday" / "Diwali (Aarav's on your list)" instead. Keeping the
two namespaces apart prevents one template's wording from contaminating
the other.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.occasion import OccasionType
from app.models.campaign_context import CampaignContext

logger = logging.getLogger("wishkr.whatsapp_templates")


# ─── Approved Meta template names ────────────────────────────────────────
# Keep in sync with WhatsApp Manager → Message templates. A typo here =
# 404 from Meta and the dispatcher refuses to fall back to free-text
# (incident 2026-04-29).
TEMPLATE_FRIEND_OCCASION_REMINDER = "occasion_reminder_notification"
TEMPLATE_VOUCHER_DELIVERED = "voucher_delivered"
TEMPLATE_VOUCHER_DELIVERED_FRIEND = "voucher_delivered_friend"
TEMPLATE_ORDER_CONFIRMATION = "order_confirmation"
TEMPLATE_PAYMENT_FAILED = "payment_failed"
TEMPLATE_MONTHLY_OCCASION_SUMMARY = "monthly_occasion_summary"
TEMPLATE_VENDOR_ORDER_NOTIFICATION = "vendor_order_notification"

# Utility templates for sends that routinely land outside the 24h CS window
# (crons, late Razorpay webhooks, founder alerts). Bodies as approved in Meta:
#   wishlist_reminder: "Hi {{1}}, this is the reminder you scheduled on
#       Wishkr for your saved item: {{2}}. Reply to this message to get the
#       saved link."  (First submission as wishlist_item_reminder was
#       classified Marketing — the "Reply SUGGEST for gift ideas" upsell
#       did it. Keep template copy strictly transactional; the suggestion
#       CTA lives in the free-text follow-up once the user replies.)
#   account_notification:   "Hi {{1}}, here's an update from Wishkr: {{2}}.
#       Reply HELP if you need assistance."  (Meta forbids a variable at the
#       very start/end of a body, hence the static tail.)
#   refund_update:          "Hi {{1}}, your refund of ₹{{2}} for order #{{3}}
#       has been {{4}}. It should reflect in 5-7 business days."
#   order_status_update:    "Hi {{1}}, an update on your order #{{2}}: {{3}}.
#       Reply HELP if you have questions."
TEMPLATE_WISHLIST_REMINDER = "wishlist_reminder"
TEMPLATE_ACCOUNT_NOTIFICATION = "account_notification"
TEMPLATE_REFUND_UPDATE = "refund_update"
TEMPLATE_ORDER_STATUS_UPDATE = "order_status_update"
#   occasion_today_update: "Hi {{1}}, an update on the occasion you saved on
#       Wishkr: your {{2}} is today. The friend reminders you set up have
#       been processed as scheduled."  (Deliberately a status update, not a
#       "happy birthday" greeting — greetings get classified Marketing.)
TEMPLATE_OCCASION_TODAY_UPDATE = "occasion_today_update"
#   preference_request: "Hi, {{1}} has asked Wishkr to collect your gift
#       preferences for an upcoming occasion. Reply to this message with the
#       kinds of gifts you like, and only your answer will be shared with
#       them. If you'd rather not share, you can ignore this message."
TEMPLATE_PREFERENCE_REQUEST = "preference_request"
#   gift_delivered_update (Utility): "Hi {{1}}, your gift card for {{2}} was
#       delivered on WhatsApp today. Reply HELP if anything looks wrong."
TEMPLATE_GIFT_DELIVERED_UPDATE = "gift_delivered_update"
#   wishkr_invite (MARKETING — costs more per send, block-rate sensitive):
#       "Hi {{1}}, sending a gift card on WhatsApp takes under a minute with
#       Wishkr. Know someone with a birthday or occasion coming up? Reply
#       GIFT and we'll help you pick one. 🎁"
TEMPLATE_WISHKR_INVITE = "welcome_invite"

# ─── Cold-acquisition first-touch templates (MARKETING) ──────────────────
# Each targets a different pain point/hook so the inbound "Hi" reply can
# message-match (see ACQUISITION_GREETING_COPY below + the greeting
# short-circuit in agent.py). Registered in Meta; this repo does not send
# them directly — an external campaign tool calls Meta and then
# ``send_template_and_record`` (or an in-repo cron, in future) to persist
# which variant a phone was sent, so the inbound reply can match it.
ACQUISITION_TEMPLATE_NAMES = frozenset({
    "welcome_invite",
    "wishkr_marketing_invite",
    "welcome_problem_reminder",
    "welcome_problem_gifts",
    "welcome_problem_duplicate",
    "welcome_problem_preferences",
    "welcome_problem_lastminute",
    "welcome_allinone",
    "welcome_curiosity",
    "welcome_beta",
    "welcome_nevermiss",
    "welcome_relationships",
    "reengage_inactive_days",
})


# ─── Per-occasion config ─────────────────────────────────────────────────

@dataclass(frozen=True)
class OccasionTemplateConfig:
    """How one ``OccasionType`` renders in WhatsApp template params.

    ``label``       — display name as it should appear in the template
                      body (e.g. "Birthday", "Diwali", "Mother's Day").
    ``is_personal`` — True for occasions that belong to a person
                      (birthday/anniversary/wedding) → friend reminder
                      uses possessive: "Aarav's Birthday".
                      False for shared festivals (Diwali, Holi, …) →
                      friend reminder mentions celebrant in parens:
                      "Diwali (Aarav's on your list)".
    """
    label: str
    is_personal: bool


OCCASION_TEMPLATE_CONFIG: dict[OccasionType, OccasionTemplateConfig] = {
    OccasionType.BIRTHDAY:       OccasionTemplateConfig(label="Birthday",       is_personal=True),
    OccasionType.ANNIVERSARY:    OccasionTemplateConfig(label="Anniversary",    is_personal=True),
    OccasionType.WEDDING:        OccasionTemplateConfig(label="Wedding",        is_personal=True),
    OccasionType.DIWALI:         OccasionTemplateConfig(label="Diwali",         is_personal=False),
    OccasionType.RAKSHA_BANDHAN: OccasionTemplateConfig(label="Raksha Bandhan", is_personal=False),
    OccasionType.VALENTINES:     OccasionTemplateConfig(label="Valentine's Day", is_personal=False),
    OccasionType.HOLI:           OccasionTemplateConfig(label="Holi",           is_personal=False),
    OccasionType.EID:            OccasionTemplateConfig(label="Eid",            is_personal=False),
    OccasionType.CHRISTMAS:      OccasionTemplateConfig(label="Christmas",      is_personal=False),
    OccasionType.NEW_YEAR:       OccasionTemplateConfig(label="New Year",       is_personal=False),
    OccasionType.MOTHERS_DAY:    OccasionTemplateConfig(label="Mother's Day",   is_personal=False),
    OccasionType.FATHERS_DAY:    OccasionTemplateConfig(label="Father's Day",   is_personal=False),
    # CUSTOM is handled at runtime — uses the user-supplied label and
    # always renders as personal ("Aarav's <label>").
}


def _resolve_label(occasion_type: OccasionType, custom_label: str | None) -> str:
    if occasion_type == OccasionType.CUSTOM:
        return (custom_label or "Special day").strip() or "Special day"
    cfg = OCCASION_TEMPLATE_CONFIG.get(occasion_type)
    return cfg.label if cfg else "Special day"


def _is_personal(occasion_type: OccasionType) -> bool:
    if occasion_type == OccasionType.CUSTOM:
        return True  # custom labels are always tied to one person
    cfg = OCCASION_TEMPLATE_CONFIG.get(occasion_type)
    return cfg.is_personal if cfg else True


# ─── Helpers used by dispatchers ─────────────────────────────────────────

def friend_reminder_phrase(
    occasion_type: OccasionType,
    custom_label: str | None,
    celebrant_name: str,
) -> str:
    """Render the {{1}} param of ``occasion_reminder_notification``.

    Reads as the subject of "... is coming up in {{2}}":
      Personal: "Aarav's Birthday"
      Shared:   "Diwali (Aarav's on your list)"
    """
    label = _resolve_label(occasion_type, custom_label)
    name = (celebrant_name or "Your friend").strip() or "Your friend"
    if _is_personal(occasion_type):
        return f"{name}'s {label}"
    return f"{label} ({name}'s on your list)"


# ─── Day-of celebrant greetings ──────────────────────────────────────────
# Sent as free-text inside the celebrant's CS window on the morning of
# their occasion. Per-type wording so it reads naturally — birthdays get a
# year-ahead wish, anniversaries a "many more years", festivals their
# culturally-appropriate greeting (Eid Mubarak, Merry Christmas, etc.).
#
# Format: (salutation, body line, emoji). Salutation is followed by ", {first_name}!"
# at render time, body line stands alone, emoji bookends the salutation.
#
# Adding a new OccasionType: add an entry here AND in
# OCCASION_TEMPLATE_CONFIG above. Both keep _resolve_label / greetings
# in sync.

@dataclass(frozen=True)
class GreetingConfig:
    salutation: str
    body: str
    emoji: str


_GREETING_CONFIG: dict[OccasionType, GreetingConfig] = {
    OccasionType.BIRTHDAY: GreetingConfig(
        salutation="Happy birthday",
        body="Wishing you a wonderful year ahead.",
        emoji="🎂",
    ),
    OccasionType.ANNIVERSARY: GreetingConfig(
        salutation="Happy anniversary",
        body="Wishing you many more years of happiness together.",
        emoji="💝",
    ),
    OccasionType.WEDDING: GreetingConfig(
        salutation="Congratulations on your wedding",
        body="Wishing you a beautiful journey ahead.",
        emoji="💍",
    ),
    OccasionType.DIWALI: GreetingConfig(
        salutation="Happy Diwali",
        body="May your day be filled with light, love, and laughter.",
        emoji="🪔",
    ),
    OccasionType.RAKSHA_BANDHAN: GreetingConfig(
        salutation="Happy Raksha Bandhan",
        body="Wishing you a day full of love and togetherness.",
        emoji="🎀",
    ),
    OccasionType.VALENTINES: GreetingConfig(
        salutation="Happy Valentine's Day",
        body="Wishing you a day full of love.",
        emoji="💐",
    ),
    OccasionType.HOLI: GreetingConfig(
        salutation="Happy Holi",
        body="May your day be full of colours and joy.",
        emoji="🎨",
    ),
    OccasionType.EID: GreetingConfig(
        salutation="Eid Mubarak",
        body="Wishing you and your family peace and blessings.",
        emoji="🌙",
    ),
    OccasionType.CHRISTMAS: GreetingConfig(
        salutation="Merry Christmas",
        body="Wishing you joy and warmth this festive season.",
        emoji="🎄",
    ),
    OccasionType.NEW_YEAR: GreetingConfig(
        salutation="Happy New Year",
        body="Wishing you a wonderful year ahead.",
        emoji="🎊",
    ),
    OccasionType.MOTHERS_DAY: GreetingConfig(
        salutation="Happy Mother's Day",
        body="Hope you have a beautiful day.",
        emoji="💐",
    ),
    OccasionType.FATHERS_DAY: GreetingConfig(
        salutation="Happy Father's Day",
        body="Hope you have a wonderful day.",
        emoji="💙",
    ),
}


def format_celebrant_greeting(
    occasion_type: OccasionType,
    custom_label: str | None,
    celebrant_name: str | None,
) -> str:
    """Build the full free-text greeting body for the celebrant.

    CUSTOM occasions can't be hard-coded — fall back to a safe phrasing
    that quotes the user-supplied label. Empty / whitespace labels degrade
    to a generic "special day" so the message never reads as broken.
    """
    first_name = _first_name(celebrant_name)
    cfg = _GREETING_CONFIG.get(occasion_type)

    if cfg is not None:
        return (
            f"{cfg.salutation}, {first_name}! {cfg.emoji}\n\n"
            f"{cfg.body}\n\n"
            "— Wishkr"
        )

    # CUSTOM or any future enum value not yet mapped here.
    safe_label = (custom_label or "").strip() or "special day"
    return (
        f"Hi {first_name}! 🎉\n\n"
        f"Hope your {safe_label} is wonderful today.\n\n"
        "— Wishkr"
    )


def days_phrase(days_until: int) -> str:
    """Render the {{2}} param so the template never reads '0 days'."""
    if days_until <= 0:
        return "today"
    if days_until == 1:
        return "1 day"
    return f"{days_until} days"


def _first_name(full: str | None, fallback: str = "there") -> str:
    name = (full or "").strip()
    if not name:
        return fallback
    return name.split()[0]


def friend_reminder_params(
    recipient_name: str | None,
    occasion_type: OccasionType,
    custom_label: str | None,
    celebrant_name: str,
    days_until: int,
) -> list[str]:
    """Build the positional body_params list for ``occasion_reminder_notification``.

    Order matches the approved Meta template body:
      "Hi {{1}}, just a reminder that {{2}}'s {{3}} is coming up in {{4}}.
       Please plan accordingly."

      {{1}} = recipient first name (e.g. "Aarav")
      {{2}} = celebrant name       (e.g. "Karan")
      {{3}} = occasion label       (e.g. "Birthday", "Anniversary")
      {{4}} = days phrase          (e.g. "3 days" / "today" / "1 day")

    Preferences are NOT passed as a template variable — Meta classifies
    qualitative lists of likes as Marketing content. They live in the
    follow-up free-text reply (sent inside the 24h CS window after the
    friend replies), where there is no Meta review and no per-message cost.
    """
    name = (celebrant_name or "Your friend").strip() or "Your friend"
    label = _resolve_label(occasion_type, custom_label)
    return [
        _first_name(recipient_name),
        name,
        label,
        days_phrase(days_until),
    ]


# ─── Money / count formatting ────────────────────────────────────────────
# Meta strips leading currency symbols from variables ("₹500" → "500" in
# some renders) and rejects edits that change variable shape, so we pass
# bare integers and keep ₹ in the static template body.

def _money(amount_rupees: Decimal | int | float | None, fallback: str = "0") -> str:
    """Render a rupees amount as a comma-grouped string ("500", "1,250", "1,00,000").

    Rupees, not paise — matches the DB convention in this codebase
    (``Order.total_amount`` and ``Purchase.amount`` are ``Decimal`` rupees).
    Razorpay webhook payloads carry paise; convert (``int(paise) // 100``)
    at the call site before passing in.

    Indian comma grouping (1,00,000 not 100,000) since the templates are
    India-only. Fractions are dropped — gift card amounts are always
    integer rupees, and the template body has no place for paise.
    """
    if amount_rupees is None:
        return fallback
    rupees = int(amount_rupees)
    s = str(rupees)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.append(head[-2:])
        head = head[:-2]
    parts.append(head)
    return ",".join(reversed(parts)) + "," + tail


def _count_phrase(n: int, singular: str, plural: str) -> str:
    """Render '1 occasion' / '3 occasions' so the template never says '1 occasions'."""
    return f"1 {singular}" if n == 1 else f"{n} {plural}"


# ─── Order confirmation ──────────────────────────────────────────────────

def order_confirmation_params(
    sender_name: str | None,
    order_id: str,
    brand: str,
    amount_rupees: Decimal | int,
    recipient_label: str,
    delivery_date: str,
    paid_amount_rupees: Decimal | int,
    payment_ref: str,
) -> list[str]:
    """Body params for ``order_confirmation``.

      "Hi {{1}}, your order #{{2}} is confirmed.
       {{3}} gift card worth ₹{{4}} will be delivered to {{5}} on {{6}}.
       Amount paid: ₹{{7}}
       Payment ref: {{8}}"

    ``recipient_label`` should be a pre-formatted "Name (phone)" string —
    formatting belongs to the caller because masking rules differ between
    self-purchase ("you") and friend-purchase ("Aarav (91XX...)").
    """
    return [
        _first_name(sender_name),
        order_id,
        brand,
        _money(amount_rupees),
        recipient_label,
        delivery_date,
        _money(paid_amount_rupees),
        payment_ref,
    ]


# ─── Payment failed ──────────────────────────────────────────────────────

# Keep failure reasons short and human — Meta has rejected templates whose
# samples leaked raw error codes ("ERR_5023") because reviewers read it as
# system noise rather than user-facing utility content.
_PAYMENT_REASON_FALLBACK = "Bank declined the payment"


def payment_failed_params(
    sender_name: str | None,
    order_id: str,
    reason: str | None,
) -> list[str]:
    """Body params for ``payment_failed``.

      "Hi {{1}}, your payment for order #{{2}} could not be completed.
       Reason: {{3}}
       Your order is on hold. Reply RETRY ... or CANCEL ..."
    """
    cleaned = (reason or "").strip()
    return [
        _first_name(sender_name),
        order_id,
        cleaned or _PAYMENT_REASON_FALLBACK,
    ]


# ─── Vendor order notification ───────────────────────────────────────────

def vendor_order_notification_params(
    order_id: str,
    items_summary: str,
    customer_name: str,
    customer_phone: str,
    shipping_address: str,
    customer_message: str | None,
    delivery_date: str,
) -> list[str]:
    """Body params for ``vendor_order_notification``.

      "You've received a new Wishkr order.
       Order #{{1}}: {{2}}.
       Customer: {{3}}.
       Ship to: {{4}}.
       Note: {{5}}.
       Delivery Date on/before: {{6}}
       Reply HELP if you have questions about this order."

    name+phone are combined into one param ({{3}}) to keep the variable
    count manageable — Meta rejected an earlier 6-variable draft that put
    them in separate slots as too dense for the body length.
    """
    return [
        order_id,
        items_summary,
        f"{customer_name} ({customer_phone})",
        shipping_address,
        (customer_message or "").strip() or "—",
        delivery_date,
    ]


# ─── Monthly occasion summary ────────────────────────────────────────────

def monthly_summary_params(
    user_name: str | None,
    occasion_count: int,
    month_label: str,
    next_occasion_phrase: str,
    next_date_label: str,
) -> list[str]:
    """Body params for ``monthly_occasion_summary``.

      "Hi {{1}}, you have {{2}} saved on Wishkr for {{3}}.
       Next up: {{4}} on {{5}}.
       Reply LIST to view all, or SKIP to pause reminders for this month."

    Caller must guarantee ``occasion_count >= 1`` — sending this template
    when there are zero saved occasions would read as marketing ("come
    back!") and risks reclassification.
    """
    return [
        _first_name(user_name),
        _count_phrase(occasion_count, "occasion", "occasions"),
        month_label,
        next_occasion_phrase,
        next_date_label,
    ]


# ─── Message-match: inbound greeting copy per acquisition template ──────
# Consumed by the greeting short-circuit in agent.py for unregistered
# phones with a fresh, unconsumed CampaignContext row. Every entry ends
# on the same "what should I call you?" beat so register_user can be
# called on the very next turn without re-asking (see prompt.py
# ONBOARDING). Unlisted/unknown template names fall back to the
# _DEFAULT_UNREGISTERED_GREETING key.
_DEFAULT_UNREGISTERED_GREETING = "_default"

ACQUISITION_GREETING_COPY: dict[str, str] = {
    "welcome_problem_reminder": (
        "Hi 👋\n"
        "Let's make sure you never miss an important occasion again.\n"
        "First, what should I call you?"
    ),
    "welcome_problem_gifts": (
        "Hi 👋\n"
        "I'll help make gift decisions less confusing.\n"
        "First, what should I call you?"
    ),
    "welcome_problem_duplicate": (
        "Hi 👋\n"
        "I'll help you avoid buying the same gift someone else already picked.\n"
        "First, what should I call you?"
    ),
    "welcome_problem_preferences": (
        "Hi 👋\n"
        "I'll help you understand what someone actually likes before you buy.\n"
        "First, what should I call you?"
    ),
    "welcome_problem_lastminute": (
        "Hi 👋\n"
        "Let's save your first occasion before it becomes last-minute panic.\n"
        "First, what should I call you?"
    ),
    "welcome_beta": (
        "Hi 👋\n"
        "Thanks for trying Wishkr early.\n"
        "First, what should I call you?"
    ),
    "welcome_curiosity": (
        "Hi 👋\n"
        "You're in. I'll show you how Wishkr helps people become better gift givers.\n"
        "First, what should I call you?"
    ),
    _DEFAULT_UNREGISTERED_GREETING: (
        "Hi 👋 I'm Wishkr.\n\n"
        "I help you remember important dates and make gifting less "
        "confusing — right here on WhatsApp.\n\n"
        "First, what should I call you?"
    ),
}
# welcome_allinone, welcome_invite, wishkr_marketing_invite, welcome_nevermiss,
# welcome_relationships, reengage_inactive_days, and any future/unrecognized
# template name all resolve to the default copy above.


def acquisition_greeting_for(template_name: str | None) -> str:
    """Resolve the unregistered-user greeting for a given campaign template."""
    if not template_name:
        return ACQUISITION_GREETING_COPY[_DEFAULT_UNREGISTERED_GREETING]
    return ACQUISITION_GREETING_COPY.get(
        template_name, ACQUISITION_GREETING_COPY[_DEFAULT_UNREGISTERED_GREETING]
    )


# ─── Send + record (message-match write path) ────────────────────────────

async def record_campaign_context(
    db: AsyncSession,
    phone: str,
    *,
    template_name: str,
    campaign_source: str | None = None,
) -> None:
    """Persist that ``template_name`` was just sent to ``phone``.

    Upsert: a new send always overwrites any prior unconsumed context for
    this phone and resets consumed_at, since the newest template is the
    one the user is now responding to. Does not commit — caller owns the
    transaction, consistent with the rest of this codebase's tool/job
    pattern. Shared by ``send_template_and_record`` (in-repo senders) and
    the ``POST /internal/campaign-context`` endpoint (external senders).
    """
    stmt = pg_insert(CampaignContext).values(
        phone=phone,
        template_name=template_name,
        campaign_source=campaign_source,
        sent_at=func.now(),
    ).on_conflict_do_update(
        index_elements=[CampaignContext.phone],
        set_={
            "template_name": template_name,
            "campaign_source": campaign_source,
            "sent_at": func.now(),
            "consumed_at": None,
        },
    )
    await db.execute(stmt)
    logger.info("campaign_context recorded phone=%s template=%s", phone[-4:], template_name)


async def send_template_and_record(
    db: AsyncSession,
    phone: str,
    *,
    template_name: str,
    body_params: list[str],
    campaign_source: str | None = None,
) -> dict:
    """Send a Meta template and, on success, persist the campaign context.

    Use this instead of calling ``whatsapp_service.send_template`` directly
    for any acquisition/first-touch template — it's the only place in this
    repo that writes ``CampaignContext``, which the inbound greeting
    short-circuit reads to message-match the first reply.
    ``whatsapp_service`` itself stays a pure HTTP client with no DB
    dependency; this wrapper is the DB-aware layer campaign-sending code
    should call.
    """
    from app.services.whatsapp import whatsapp_service  # local import: avoid whatsapp.py depending on app.models

    result = await whatsapp_service.send_template(
        phone, template_name=template_name, body_params=body_params,
    )
    await record_campaign_context(
        db, phone, template_name=template_name, campaign_source=campaign_source,
    )
    return result
