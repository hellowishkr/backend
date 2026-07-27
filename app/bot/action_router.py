"""Deterministic pre-LLM routing — high-confidence paths only, no main agent.

Product links are handled *before* intent classification, but only when
there is a high-confidence save signal (explicit intent phrase, active
awaiting state, or a bare link in friend-flow). Ambiguous product-URL
messages (e.g. "is this worth buying?") are passed through to the main
agent so context is not lost.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.canned_responses import (
    MENU_RESPONSE,
    PRODUCT_GATE_RESPONSE,
    PURCHASE_DISABLED_RESPONSE,
    WISHLIST_ADDED_RESPONSE,
    WISHLIST_AFFIRMATIVE_REPROMPT,
    WISHLIST_DUPLICATE_RESPONSE,
    WISHLIST_LIMIT_RESPONSE,
    format_wishlist,
)
from app.core.config import settings
from app.bot.intent_classifier import IntentResult, is_rule_based_affirmative
from app.bot.language_map import (
    MENU_EXACT_PHRASES,
    MENU_WORDS,
    OCCASION_LIST_EXACT_PHRASES,
    has_explicit_save_intent,
    looks_like_occasion_list_request,
    normalize_text,
)
from app.bot.messages import append_message
from app.bot.state_utils import (
    clear_state,
    get_awaiting,
    get_gift_need_profile,
    get_state,
    has_active_gift_context,
    set_state,
)
from app.bot.tools.tool_impl.cart import view_cart
from app.bot.tools.tool_impl.occasions import get_my_occasions
from app.bot.tools.tool_impl.orders import list_orders
from app.bot.tools.tool_impl.wishlist import remove_wishlist_item
from app.bot.wishlist_store import add_wishlist_product_url, list_wishlist_items
from app.models import MessageRole, Occasion, User, WishlistItem
from app.utils.link_utils import extract_urls, is_product_link
from app.utils.link_utils import normalize_url
from sqlalchemy import select
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("wishkr.router")

_ORDERS_PHRASES = frozenset({
    "my orders", "show my orders", "show orders", "orders", "order status",
})
_PAYMENT_STATUS_PHRASES = frozenset({
    "payment status", "show payment status", "payment update", "payment check",
})
_CART_PHRASES = frozenset({
    "cart", "show cart", "my cart", "cart summary", "view cart",
})
_PURCHASE_REQUEST_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bbuy(?:ing)?\b",
        r"\bsend\s+(?:a\s+)?gift\b",
        r"\bgift\s+card(s)?\b",
        r"\bvoucher(s)?\b",
        r"\b(?:can|could|want|wanna|please|create|place|make|start)\b.*\border(?:ing)?\b",
        r"\border\s+(?:this|it|now|a\s+gift|the\s+gift)\b",
        r"\bcheckout\b",
    )
)
_PURCHASE_REQUEST_PHRASES = (
    "create payment link",
    "payment link",
)
_PURCHASE_REQUEST_EXACTS = frozenset({"order"})
_HELP_PHRASES = MENU_WORDS

# Tight set of "the user really meant the system menu" phrases. Used to gate
# the canned MENU_RESPONSE when the LLM classifier returns ``menu_request``
# for a longer message like "Can you suggest me 10/15 more options" — those
# should fall through to the agent with the active flow intact, not dump
# the cold-start menu and lose recipient/gift context. See the Swaraj/BGMI
# conversation post-mortem.
_MENU_EXACT_PHRASES = MENU_EXACT_PHRASES
_OCCASION_LIST_EXACT_PHRASES = OCCASION_LIST_EXACT_PHRASES


def _has_explicit_save_intent(text: str) -> bool:
    """Return True when the message contains an unambiguous save phrase."""
    return has_explicit_save_intent(text)


def _looks_like_link_only(text: str, urls: list[str]) -> bool:
    """Return True when the message is essentially just a URL with no surrounding question.

    Strips all URLs from the text and checks whether what remains is very
    short (≤ 6 non-whitespace chars — covers greetings like 'hi' or
    decorators like '👇' but not a question or sentence).
    """
    remaining = text
    for url in urls:
        remaining = remaining.replace(url, "")
    return len(remaining.strip().replace(" ", "")) <= 6


def _normalize_text(text: str) -> str:
    return normalize_text(text)


async def _active_gift_profile(db: AsyncSession, phone: str):
    state = await get_state(db, phone)
    profile = get_gift_need_profile(state)
    if profile and profile.is_recent():
        return profile
    return None


async def _has_active_gift_for_friend_flow(
    db: AsyncSession, user: User | None, phone: str | None = None,
) -> bool:
    """Heuristic: the caller is mid-flow buying a gift for a friend.

    True when the user owns any contact-bound occasion whose next
    occurrence is within +/- 14 days (i.e. they likely just added Swaraj's
    birthday and are now asking "headphones?" / "brands for headphones?").
    In that state, dumping the canned "add to YOUR wishlist" product gate
    is the wrong answer — let the main agent answer with full context.
    """
    if phone:
        profile = await _active_gift_profile(db, phone)
        if profile and profile.is_gift_for_friend:
            return True
    if user is None:
        return False
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=14)
    window_end = now + timedelta(days=14)
    # Date comparison is on month/day only (occasions repeat) — but the
    # naive check below is good enough: most recent contact occasions get
    # written with this year's date or next year's date by add_occasion.
    rows = await db.execute(
        select(Occasion.id, Occasion.date, Occasion.contact_id)
        .where(Occasion.user_id == user.id)
        .where(Occasion.contact_id.is_not(None))
        .where(Occasion.deleted_at.is_(None))
    )
    for _id, occ_date, _contact_id in rows.all():
        if occ_date is None:
            continue
        # Some date columns may be naive — normalise both sides.
        occ_aware = occ_date if occ_date.tzinfo else occ_date.replace(tzinfo=timezone.utc)
        if window_start <= occ_aware <= window_end:
            return True
        # Recurring → also check this year's occurrence relative to now.
        try:
            this_year = occ_aware.replace(year=now.year)
        except ValueError:
            continue
        if window_start <= this_year <= window_end:
            return True
    return False


def _is_occasion_list_request(norm: str) -> bool:
    """True for explicit requests to list the caller's saved occasions."""
    return looks_like_occasion_list_request(norm)


def _format_my_occasions_reply(payload: dict) -> str:
    occasions = payload.get("occasions") or []
    if not occasions:
        return (
            "I don't have any occasions saved for you yet. Tell me the person, "
            "occasion, and date, and I'll remember it."
        )

    lines = ["Here are the occasions I have saved for you:"]
    for i, item in enumerate(occasions, start=1):
        whose = item.get("whose") or item.get("contact_name") or "Someone"
        relation = item.get("relation") or item.get("relationship")
        occasion = item.get("occasion") or "Occasion"
        date = item.get("date") or "date not set"
        bits = [str(whose)]
        if relation:
            bits.append(f"({relation})")
        bits.append(f"- {occasion}")
        if date:
            bits.append(f"- {date}")
        lines.append(f"{i}. {' '.join(bits)}")
    return "\n".join(lines)


def _extract_wishlist_remove_target(text: str) -> tuple[str | None, str | None]:
    """Return (url, item_id) only for explicit remove/delete wishlist intents."""
    norm = _normalize_text(text)
    if not any(word in norm for word in ("remove", "delete")):
        return None, None
    if "wishlist" not in norm:
        return None, None

    urls = extract_urls(text)
    for raw in urls:
        normalized = normalize_url(raw) or raw
        if normalized:
            return normalized, None

    for token in text.replace("\n", " ").split():
        t = token.strip(".,;:()[]{}<>")
        if t.startswith("wli_"):
            return None, t
    return None, None


def _format_orders_reply(payload: dict) -> str:
    orders = payload.get("orders") or []
    if not orders:
        return "No orders yet."
    lines = ["Your recent orders:"]
    for i, item in enumerate(orders[:10], start=1):
        brand = item.get("brand") or "Unknown brand"
        amount = item.get("amount")
        status = (item.get("status") or "unknown").lower()
        date = item.get("date") or ""
        amount_text = f"₹{int(amount)}" if isinstance(amount, (int, float)) else str(amount or "")
        lines.append(f"{i}. {brand} {amount_text} — {status}{f' ({date})' if date else ''}")
    return "\n".join(lines)


def _format_cart_reply(payload: dict) -> str:
    items = payload.get("cart") or []
    if not items:
        return payload.get("message") or "Cart is empty."
    lines = [f"Your cart ({payload.get('count', len(items))} items):"]
    for i, item in enumerate(items, start=1):
        brand = item.get("brand_name") or "Unknown brand"
        amount = item.get("amount") or 0
        lines.append(f"{i}. {brand} ₹{amount}")
    lines.append(f"Total: ₹{payload.get('total', 0)}")
    return "\n".join(lines)


async def try_handle_product_urls_before_classifier(
    db: AsyncSession,
    phone: str,
    text: str,
    *,
    celebrant_occasion_id: str | None = None,
) -> str | None:
    """Conservative wishlist link path — runs before `classify()` to avoid LLM latency.

    Saves the link only when there is a high-confidence signal that the user
    wants to save it, not merely share or ask about it:

      a) ``awaiting == "wishlist_link"`` — user was explicitly prompted for a link.
      b) ``_has_explicit_save_intent(text)`` — message contains a save phrase.
      c) ``celebrant_occasion_id`` is set AND the message is essentially just
         a bare URL (friend sharing a gift link with no surrounding question).

    All other product-URL messages (e.g. "is this worth buying?") return None
    so the main agent can handle the question with full context.

    When the user is in friend-flow (an active celebrant context), the save is
    recorded as ``purpose='gift'`` against that occasion so it doesn't pollute
    the saver's personal wishlist.
    """
    urls = extract_urls(text)
    product_urls = [u for u in urls if is_product_link(u)]
    if not product_urls:
        return None

    awaiting = await get_awaiting(db, phone)

    should_save = (
        awaiting == "wishlist_link"
        or _has_explicit_save_intent(text)
        or (celebrant_occasion_id is not None and _looks_like_link_only(text, product_urls))
    )

    if not should_save:
        logger.info(
            "[%s] router=preflight product_url skipped (no save signal) — deferring to agent",
            phone,
        )
        return None

    purpose = "gift" if celebrant_occasion_id else "self"
    recipient_occasion_id = celebrant_occasion_id
    if not recipient_occasion_id:
        profile = await _active_gift_profile(db, phone)
        if profile and profile.is_gift_for_friend:
            purpose = "gift"
            recipient_occasion_id = profile.occasion_id
            if not recipient_occasion_id:
                logger.info(
                    "[%s] router=preflight blocked product_url_save missing active recipient occasion",
                    phone,
                )
                return (
                    "I still know this is for your friend, but I need the occasion "
                    "context before saving this gift idea. Tell me which occasion this is for."
                )
    logger.info(
        "[%s] router=preflight product_url_save purpose=%s rcpt=%s awaiting=%s",
        phone, purpose, recipient_occasion_id, awaiting,
    )
    return await _complete_wishlist_link_turn(
        db, phone, text, product_urls[0],
        purpose=purpose,
        recipient_occasion_id=recipient_occasion_id,
    )


async def try_handle_actions(
    db: AsyncSession,
    phone: str,
    text: str,
    *,
    user: User | None,
    intent_result: IntentResult,
) -> str | None:
    """If this turn is handled without the main LLM, return the reply; else None."""
    norm = _normalize_text(text)

    profile = await _active_gift_profile(db, phone)
    active_gift_flow = bool(profile and profile.is_gift_for_friend)

    if settings.mvp_reminder_only and (
        any(pattern.search(norm) for pattern in _PURCHASE_REQUEST_PATTERNS)
        or any(phrase in norm for phrase in _PURCHASE_REQUEST_PHRASES)
        or norm in _PURCHASE_REQUEST_EXACTS
        or norm in _ORDERS_PHRASES
        or norm in _PAYMENT_STATUS_PHRASES
    ):
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=PURCHASE_DISABLED_RESPONSE)
        logger.info("[%s] router=mvp_purchase_disabled", phone)
        return PURCHASE_DISABLED_RESPONSE

    if norm in _HELP_PHRASES or (norm == "options" and not active_gift_flow):
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=MENU_RESPONSE)
        logger.info("[%s] router=help_menu", phone)
        return MENU_RESPONSE

    if intent_result.intent == "menu_request" and user and user.name:
        # LLM classifier sometimes maps mid-flow phrases like "suggest me 10
        # more options" / "any options for gaming?" to menu_request because
        # of the word "options". Cold-dumping MENU_RESPONSE in that case
        # blows away the gift flow (recipient, occasion, interest, budget)
        # the user just established. Only fire the canned menu for tight
        # menu-like phrases; otherwise pass through to the main agent.
        if norm in _MENU_EXACT_PHRASES and not (active_gift_flow and "option" in norm):
            await append_message(db, phone, MessageRole.USER, content=text)
            await append_message(db, phone, MessageRole.MODEL, content=MENU_RESPONSE)
            logger.info("[%s] router=menu", phone)
            return MENU_RESPONSE
        logger.info(
            "[%s] router=menu_request_passthrough text=%r — letting agent continue active flow",
            phone, norm[:80],
        )

    if norm in _ORDERS_PHRASES or norm in _PAYMENT_STATUS_PHRASES:
        result = await list_orders(db, phone, {})
        reply = _format_orders_reply(result)
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=reply)
        logger.info("[%s] router=orders_or_payment count=%d", phone, len(result.get("orders") or []))
        return reply

    if norm in _CART_PHRASES:
        result = await view_cart(db, phone, {})
        reply = _format_cart_reply(result)
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=reply)
        logger.info("[%s] router=cart_summary count=%d", phone, len(result.get("cart") or []))
        return reply

    if intent_result.intent == "show_wishlist":
        items = await list_wishlist_items(db, phone)
        reply = format_wishlist([(it.title, it.url) for it in items])
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=reply)
        logger.info("[%s] router=show_wishlist count=%d", phone, len(items))
        return reply

    if intent_result.intent == "show_occasions" or _is_occasion_list_request(norm):
        result = await get_my_occasions(db, phone, {})
        reply = _format_my_occasions_reply(result)
        await append_message(db, phone, MessageRole.USER, content=text)
        await append_message(db, phone, MessageRole.MODEL, content=reply)
        logger.info(
            "[%s] router=my_occasions count=%d",
            phone, len(result.get("occasions") or []),
        )
        return reply

    remove_url, remove_item_id = _extract_wishlist_remove_target(text)
    if remove_url or remove_item_id:
        await append_message(db, phone, MessageRole.USER, content=text)
        if remove_item_id:
            owner = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
            if owner:
                item = (
                    await db.execute(
                        select(WishlistItem).where(
                            WishlistItem.id == remove_item_id,
                            WishlistItem.user_id == owner.id,
                            WishlistItem.purpose == "self",
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if item is not None:
                    await db.delete(item)
                    await db.flush()
                    reply = "Removed from wishlist."
                else:
                    reply = "Item not found in your wishlist."
            else:
                reply = "Item not found in your wishlist."
        else:
            outcome = await remove_wishlist_item(db, phone, {"url": remove_url})
            reply = outcome.get("message") or "Item not found in your wishlist."
        await append_message(db, phone, MessageRole.MODEL, content=reply)
        logger.info("[%s] router=remove_wishlist deterministic", phone)
        return reply

    awaiting = await get_awaiting(db, phone)
    if awaiting == "wishlist_link":
        # Product URLs are handled in `try_handle_product_urls_before_classifier`
        # (runs before classify in `handle_message`) so we never depend on intent
        # or the LLM classifier to save a link after the gate.

        if is_rule_based_affirmative(text):
            await append_message(db, phone, MessageRole.USER, content=text)
            await append_message(db, phone, MessageRole.MODEL, content=WISHLIST_AFFIRMATIVE_REPROMPT)
            logger.info("[%s] router=wishlist_link affirmative_reprompt", phone)
            return WISHLIST_AFFIRMATIVE_REPROMPT

        # User changed topic — clear stale awaiting state so a later unrelated
        # product URL isn't silently saved against this long-gone prompt.
        if intent_result.intent not in {"product_question", "show_wishlist"}:
            await clear_state(db, phone, "awaiting")
            logger.info(
                "[%s] router=wishlist_link stale_clear intent=%s",
                phone, intent_result.intent,
            )

        return None

    if intent_result.intent == "product_question" and intent_result.confidence >= 0.85:
        # When the user is mid-flow buying a gift for a friend (a recent
        # contact-bound occasion exists in a +/- 14 day window), don't
        # short-circuit with the cold "add to YOUR wishlist" gate — that
        # answer is wrong for "suggest headphone brands for Swaraj" and
        # was a major regression in the Swaraj/BGMI conversation. Pass
        # through to the main agent which has occasion + recipient context
        # in its dynamic tail and can answer correctly.
        if await _has_active_gift_for_friend_flow(db, user, phone):
            logger.info(
                "[%s] router=product_gate skipped — active gift-for-friend flow",
                phone,
            )
            return None
        await append_message(db, phone, MessageRole.USER, content=text)
        await set_state(db, phone, "awaiting", "wishlist_link")
        await append_message(db, phone, MessageRole.MODEL, content=PRODUCT_GATE_RESPONSE)
        logger.info("[%s] router=product_gate awaiting=wishlist_link", phone)
        return PRODUCT_GATE_RESPONSE

    return None


async def _complete_wishlist_link_turn(
    db: AsyncSession,
    phone: str,
    text: str,
    product_url: str,
    *,
    purpose: str = "self",
    recipient_occasion_id: str | None = None,
) -> str:
    await append_message(db, phone, MessageRole.USER, content=text)
    outcome = await add_wishlist_product_url(
        db, phone, product_url,
        purpose=purpose,
        recipient_occasion_id=recipient_occasion_id,
    )
    if outcome == "duplicate":
        reply = WISHLIST_DUPLICATE_RESPONSE
    elif outcome == "limit_reached":
        reply = WISHLIST_LIMIT_RESPONSE
    else:
        reply = WISHLIST_ADDED_RESPONSE
    # Clear awaiting on success or duplicate; on limit_reached the user needs
    # to remove an item before any new link will land, so dropping the gate
    # is still the right move (avoids loop on retries with the same link).
    await clear_state(db, phone, "awaiting")
    await append_message(db, phone, MessageRole.MODEL, content=reply)
    return reply
