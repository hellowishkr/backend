"""Recipient preference request flow.

request_recipient_preferences — send a preference-gathering message to
  the recipient after explicit gift-giver permission. Deduplicates against
  existing pending/recent requests to prevent spam.

save_recipient_preferences — parse and store recipient's response.
  Called by the WhatsApp webhook when the recipient replies to the
  preference-request message.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recipient_preference import PreferenceRequest, RecipientPreference
from app.utils.id_gen import generate_id
from app.utils.phones import phone_aliases

logger = logging.getLogger("wishkr.tools.recipient_preferences")

# How many days a preference request stays pending before expiring.
PREFERENCE_REQUEST_TTL_DAYS = 7
# How many days a recent response is still considered fresh (no re-request needed).
PREFERENCE_FRESH_DAYS = 30

# Canonical category labels. Shared by the keyword parser (_parse_preferences)
# and the LLM fallback (_llm_categorize) so both produce consistent values for
# preferred_categories, which downstream gift suggestion code reads.
KNOWN_CATEGORIES = [
    "gadgets", "electronics", "books", "reading", "fashion", "clothing",
    "skincare", "beauty", "food", "coffee", "tea", "travel", "experiences",
    "fitness", "gym", "yoga", "music", "movies", "home decor", "kitchen",
    "jewellery", "watches", "bags", "shoes", "sneakers", "gaming",
    "gift cards", "vouchers", "flowers", "chocolates", "sweets",
]

# Timeout for the optional LLM categorization fallback (recipient-reply path is
# async, so this never blocks an inbound user).
_CATEGORIZE_TIMEOUT_SECONDS = 4.0


def _normalize_phone(raw: str) -> str | None:
    """Normalize a phone string to canonical 12-digit Indian format."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return None


async def request_recipient_preferences(db: AsyncSession, phone: str, args: dict) -> dict:
    """Request gift preferences from a recipient after gift-giver permission.

    Deduplication rules:
    1. If a 'pending' request already exists for this (recipient, occasion),
       merge the requester into notify_phones and return the existing request.
    2. If fresh preferences already exist (within PREFERENCE_FRESH_DAYS),
       return them immediately without sending another message.
    3. Otherwise, create a new PreferenceRequest and dispatch the WhatsApp message.
    """
    raw_phone = (args.get("recipient_phone") or "").strip()
    recipient_phone = _normalize_phone(raw_phone)
    if not recipient_phone:
        return {
            "error": "invalid_phone",
            "hint": "Pass a valid Indian phone number (10-12 digits). Cannot message recipient without a valid phone.",
        }

    occasion_id = (args.get("occasion_id") or "").strip() or None
    anonymous = bool(args.get("anonymous", True))
    now = datetime.now(timezone.utc)

    # Guard: do not message the recipient without explicit permission.
    # The tool description and prompt require this check to be done conversationally
    # before calling the tool. We add a canary check here too.
    if phone == recipient_phone:
        return {
            "error": "cannot_message_self",
            "hint": "The gift-giver and recipient phone are the same. Cannot send a preference request to yourself.",
        }

    # Check for fresh preferences — no need to message again.
    existing_pref = await db.scalar(
        select(RecipientPreference).where(
            RecipientPreference.recipient_phone.in_(phone_aliases(recipient_phone))
        )
    )
    if existing_pref:
        freshness_cutoff = now - timedelta(days=PREFERENCE_FRESH_DAYS)
        updated_at = existing_pref.updated_at
        # SQLite returns naive datetimes for timezone=True columns; strip tz for
        # comparison so the test suite (SQLite) and production (Postgres) both work.
        if updated_at.tzinfo is None:
            freshness_cutoff = freshness_cutoff.replace(tzinfo=None)
        if updated_at >= freshness_cutoff:
            return {
                "status": "already_available",
                "message": "Recent preferences already exist. No new message needed.",
                "preferences": {
                    "preferred_categories": existing_pref.preferred_categories,
                    "preferred_brands": existing_pref.preferred_brands,
                    "budget_min": existing_pref.preferred_budget_min,
                    "budget_max": existing_pref.preferred_budget_max,
                    "notes": existing_pref.notes,
                },
                "collected_at": existing_pref.updated_at.isoformat(),
            }

    # Check for an existing pending request for this (recipient, occasion).
    pending_q = select(PreferenceRequest).where(
        PreferenceRequest.recipient_phone.in_(phone_aliases(recipient_phone)),
        PreferenceRequest.status == "pending",
    )
    if occasion_id:
        pending_q = pending_q.where(PreferenceRequest.occasion_id == occasion_id)
    existing_req = await db.scalar(pending_q.order_by(PreferenceRequest.created_at.desc()))

    if existing_req:
        # Merge this requester into notify_phones if not already there.
        existing_notifs: list[str] = []
        if existing_req.notify_phones:
            existing_notifs = [p.strip() for p in existing_req.notify_phones.split(",") if p.strip()]
        if phone not in existing_notifs:
            existing_notifs.append(phone)
            existing_req.notify_phones = ",".join(existing_notifs)
            db.add(existing_req)
            await db.flush()
        return {
            "status": "existing_request",
            "request_id": existing_req.id,
            "message": (
                "A preference request is already pending for this recipient. "
                "You will be notified when they respond."
            ),
        }

    # Create a new preference request.
    expires = now + timedelta(days=PREFERENCE_REQUEST_TTL_DAYS)
    req = PreferenceRequest(
        id=generate_id("prq"),
        recipient_phone=recipient_phone,
        occasion_id=occasion_id,
        requester_phone=phone,
        notify_phones=phone,
        status="pending",
        anonymous=anonymous,
        expires_at=expires,
    )
    db.add(req)
    await db.flush()

    # Send the WhatsApp preference-request message.
    # The message is deliberately vague — it does not reveal gift-giver identity
    # (anonymous=True) or the specific occasion. The recipient can share freely.
    try:
        await _send_preference_request_message(
            recipient_phone=recipient_phone,
            anonymous=anonymous,
            requester_phone=phone if not anonymous else None,
        )
        logger.info(
            "preference request sent req=%s recipient=%s anonymous=%s",
            req.id, recipient_phone[-4:], anonymous,
        )
    except Exception as exc:
        # Message send failure — mark the request for retry, but don't fail
        # the tool (the DB row is already written for dedup purposes).
        logger.error("preference request message send failed req=%s: %s", req.id, exc)
        return {
            "status": "send_failed",
            "request_id": req.id,
            "message": (
                "Could not send the preference message right now. "
                "The request is saved and will be retried."
            ),
        }

    return {
        "status": "sent",
        "request_id": req.id,
        "expires_at": expires.isoformat(),
        "anonymous": anonymous,
        "message": (
            "A message has been sent asking the recipient for their gift preferences. "
            "You will be notified when they respond."
        ),
    }


async def save_recipient_preferences(
    db: AsyncSession,
    recipient_phone: str,
    raw_response: str,
    request_id: str | None = None,
    parsed_preferences: dict | None = None,
) -> dict:
    """Parse and store a recipient's preference response. Called from the webhook.

    Parses free-text into structured preference fields. Notifies the
    original gift-givers that preferences are now available.
    """
    prefs = dict(parsed_preferences) if parsed_preferences is not None else _parse_preferences(raw_response)
    # The keyword parser misses anything outside its fixed list (e.g. "a PS5").
    # When it finds no category, ask the LLM to map the raw reply to canonical
    # labels. The raw text already lives in notes either way.
    if parsed_preferences is None and not prefs.get("preferred_categories"):
        llm_cats = await _llm_categorize(raw_response)
        if llm_cats:
            prefs["preferred_categories"] = llm_cats
    now = datetime.now(timezone.utc)

    existing = await db.scalar(
        select(RecipientPreference).where(
            RecipientPreference.recipient_phone.in_(phone_aliases(recipient_phone))
        )
    )
    if existing:
        # Update existing record.
        if prefs.get("preferred_categories"):
            existing.preferred_categories = prefs["preferred_categories"]
        if prefs.get("disliked_categories"):
            existing.disliked_categories = prefs["disliked_categories"]
        if prefs.get("preferred_brands"):
            existing.preferred_brands = prefs["preferred_brands"]
        if prefs.get("disliked_brands"):
            existing.disliked_brands = prefs["disliked_brands"]
        if prefs.get("budget_min"):
            existing.preferred_budget_min = prefs["budget_min"]
        if prefs.get("budget_max"):
            existing.preferred_budget_max = prefs["budget_max"]
        if prefs.get("notes"):
            existing.notes = prefs["notes"]
        if prefs.get("wishlist_links"):
            existing.wishlist_links = json.dumps(prefs["wishlist_links"])
        existing.source = "direct_response"
        existing.updated_at = now
        db.add(existing)
        pref_row = existing
    else:
        pref_row = RecipientPreference(
            id=generate_id("prf"),
            recipient_phone=recipient_phone,
            preferred_categories=prefs.get("preferred_categories"),
            disliked_categories=prefs.get("disliked_categories"),
            preferred_brands=prefs.get("preferred_brands"),
            disliked_brands=prefs.get("disliked_brands"),
            preferred_budget_min=prefs.get("budget_min"),
            preferred_budget_max=prefs.get("budget_max"),
            notes=prefs.get("notes"),
            wishlist_links=json.dumps(prefs["wishlist_links"]) if prefs.get("wishlist_links") else None,
            source="direct_response",
        )
        db.add(pref_row)

    # Close the pending request and collect notify_phones.
    notify_phones: list[str] = []
    if request_id:
        req = await db.get(PreferenceRequest, request_id)
        if req and req.status == "pending":
            req.status = "responded"
            req.responded_at = now
            db.add(req)
            if req.notify_phones:
                notify_phones = [p.strip() for p in req.notify_phones.split(",") if p.strip()]
    else:
        # Find all pending requests for this recipient.
        pending_reqs = (await db.execute(
            select(PreferenceRequest).where(
                PreferenceRequest.recipient_phone.in_(phone_aliases(recipient_phone)),
                PreferenceRequest.status == "pending",
            )
        )).scalars().all()
        for req in pending_reqs:
            req.status = "responded"
            req.responded_at = now
            db.add(req)
            if req.notify_phones:
                for p in req.notify_phones.split(","):
                    p = p.strip()
                    if p and p not in notify_phones:
                        notify_phones.append(p)

    await db.flush()

    # Notify each gift-giver. We import lazily to avoid circular imports.
    if notify_phones:
        await _notify_gift_givers(notify_phones, prefs)

    return {
        "saved": True,
        "preference_id": pref_row.id,
        "notified_count": len(notify_phones),
        "parsed": prefs,
    }


def _parse_preferences(raw: str) -> dict:
    """Parse a free-text preference response into structured fields.

    Categories, brands, budget hints, and wishlist links are extracted.
    Anything that can't be parsed goes into 'notes'.
    """
    import re
    raw_lower = raw.lower()

    # Known categories → canonical or near-canonical labels.
    known_cats = KNOWN_CATEGORIES
    preferred_cats = [c for c in known_cats if c in raw_lower]

    dislikes: list[str] = []
    dislike_patterns = re.findall(r"(?:don\'t like|dislike|hate|not into|avoid)\s+([a-zA-Z\s,]+?)(?:\.|,|$)", raw_lower)
    for match in dislike_patterns:
        dislikes.extend(c for c in known_cats if c in match)

    # Budget extraction — simple patterns like "under 500", "around 2000-3000"
    budget_min: int | None = None
    budget_max: int | None = None
    budget_match = re.search(r"(?:under|below|less than)\s*(?:rs\.?|₹)?\s*(\d+)", raw_lower)
    if budget_match:
        budget_max = int(budget_match.group(1))
    range_match = re.search(r"(?:rs\.?|₹)?\s*(\d+)\s*[-–to]+\s*(?:rs\.?|₹)?\s*(\d+)", raw_lower)
    if range_match:
        budget_min = int(range_match.group(1))
        budget_max = int(range_match.group(2))
    k_match = re.search(r"(\d+)k", raw_lower)
    if k_match and not budget_max:
        budget_max = int(k_match.group(1)) * 1000

    # Wishlist links (Amazon, Flipkart, etc.)
    link_pattern = re.compile(r"https?://[^\s]+")
    links = link_pattern.findall(raw)

    # Brand names — simple heuristics.
    brand_keywords = [
        "amazon", "flipkart", "myntra", "nykaa", "ajio", "starbucks", "zomato",
        "swiggy", "apple", "samsung", "boat", "sony", "nike", "adidas", "puma",
        "levi", "h&m", "zara", "fabindia", "manyavar",
    ]
    preferred_brands = [b for b in brand_keywords if b in raw_lower]

    notes = raw.strip() if not preferred_cats else None

    return {
        "preferred_categories": ", ".join(preferred_cats) if preferred_cats else None,
        "disliked_categories": ", ".join(dislikes) if dislikes else None,
        "preferred_brands": ", ".join(preferred_brands) if preferred_brands else None,
        "disliked_brands": None,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "notes": notes,
        "wishlist_links": links if links else None,
    }


_CATEGORIZE_PROMPT = (
    "A gift recipient described what they'd like to receive. Map it to gift "
    "categories from this list ONLY:\n{cats}\n\n"
    "Reply with a comma-separated subset of those exact labels that fit, or the "
    "single word NONE if nothing fits. No other words.\n\n"
    "Message: {msg}"
)


def _parse_categorize_reply(raw: str) -> str | None:
    """Keep only valid canonical labels from an LLM reply; None if nothing maps."""
    allowed = {c.lower() for c in KNOWN_CATEGORIES}
    picked = [
        part.strip().lower()
        for part in (raw or "").replace("\n", ",").split(",")
        if part.strip().lower() in allowed
    ]
    # Preserve KNOWN_CATEGORIES order and drop duplicates.
    ordered = [c for c in KNOWN_CATEGORIES if c in picked]
    return ", ".join(ordered) if ordered else None


async def _llm_categorize(raw: str) -> str | None:
    """Map free-text preferences to canonical categories via LLM.

    Used only when the keyword parser found no category. DeepSeek primary,
    Gemini fallback. Returns a comma-separated category string, or None if the
    call fails or nothing maps. Best-effort: never raises.
    """
    from app.core.config import settings

    prompt = _CATEGORIZE_PROMPT.format(
        cats=", ".join(KNOWN_CATEGORIES), msg=raw[:500]
    )

    if settings.deepseek_api_key:
        try:
            return await _deepseek_categorize(prompt)
        except Exception as exc:
            logger.warning("deepseek categorize failed (%s) — trying gemini", exc)

    if settings.gemini_api_key:
        try:
            return await _gemini_categorize(prompt)
        except Exception as exc:
            logger.warning("gemini categorize fallback failed: %s", exc)

    return None


async def _deepseek_categorize(prompt: str) -> str | None:
    import httpx

    from app.core.config import settings

    async with httpx.AsyncClient(timeout=_CATEGORIZE_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={
                "model": settings.deepseek_classifier_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 30,
            },
        )
        resp.raise_for_status()
        return _parse_categorize_reply(resp.json()["choices"][0]["message"]["content"])


async def _gemini_categorize(prompt: str) -> str | None:
    import asyncio

    from google import genai
    from google.genai import types as genai_types

    from app.core.config import settings

    client = genai.Client(api_key=settings.gemini_api_key)
    resp = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=settings.gemini_fast_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=0.0, max_output_tokens=30),
        ),
        timeout=_CATEGORIZE_TIMEOUT_SECONDS,
    )
    return _parse_categorize_reply(resp.text or "")


async def _send_preference_request_message(
    *,
    recipient_phone: str,
    anonymous: bool,
    requester_phone: str | None,
) -> None:
    """Send a WhatsApp message to the recipient asking for gift preferences.

    Recipients are usually cold contacts who have never messaged us (CS
    window closed), so this routes through the approved
    ``preference_request`` template; the richer free-text version only goes
    out when the recipient's window happens to be open.
    """
    from sqlalchemy import select

    from app.bot.whatsapp_templates import TEMPLATE_PREFERENCE_REQUEST, _first_name
    from app.db.session import async_session
    from app.models import User
    from app.services.whatsapp import send_text_or_template

    async with async_session() as db:
        row = (await db.execute(
            select(User.last_inbound_at).where(User.phone == recipient_phone)
        )).first()
        requester_name = "Someone"
        if requester_phone and not anonymous:
            requester_row = (await db.execute(
                select(User.name).where(User.phone == requester_phone)
            )).first()
            if requester_row and requester_row.name:
                requester_name = _first_name(requester_row.name)

    body = (
        "Hi! Someone wants to gift you something through Wishkr 🎁\n\n"
        "What kind of gifts would you like? You can share categories like:\n"
        "• Gadgets or electronics\n"
        "• Fashion or clothing\n"
        "• Books or music\n"
        "• Skincare or beauty\n"
        "• Coffee, chocolates, or food\n"
        "• Gift cards or vouchers\n"
        "• Or anything else!\n\n"
        "You can also share your Amazon or Flipkart wishlist link if you have one."
    )
    await send_text_or_template(
        recipient_phone,
        body,
        last_inbound_at=row.last_inbound_at if row else None,
        template_name=TEMPLATE_PREFERENCE_REQUEST,
        body_params=[requester_name],
    )


async def _notify_gift_givers(notify_phones: list[str], prefs: dict) -> None:
    """Send a WhatsApp notification to gift-givers when preferences are ready.

    Gift-givers are existing users but may not have messaged us within 24h,
    so route through the template fallback rather than bare free text.
    """
    from sqlalchemy import select

    from app.bot.whatsapp_templates import TEMPLATE_ACCOUNT_NOTIFICATION, _first_name
    from app.db.session import async_session
    from app.models import User
    from app.services.whatsapp import send_text_or_template

    cats = prefs.get("preferred_categories") or prefs.get("notes") or "their preferences"
    body = (
        f"Good news! The person you want to gift has shared their preferences 🎉\n\n"
        f"They like: {cats}\n\n"
        f"Reply 'suggest gifts' to see recommendations based on their preferences!"
    )
    async with async_session() as db:
        rows = (await db.execute(
            select(User.phone, User.name, User.last_inbound_at)
            .where(User.phone.in_(notify_phones))
        )).all()
    user_by_phone = {r.phone: r for r in rows}

    for phone in notify_phones:
        user_row = user_by_phone.get(phone)
        try:
            await send_text_or_template(
                phone,
                body,
                last_inbound_at=user_row.last_inbound_at if user_row else None,
                template_name=TEMPLATE_ACCOUNT_NOTIFICATION,
                body_params=[
                    _first_name(user_row.name if user_row else None),
                    f"The person you want to gift has shared their preferences. "
                    f"They like: {cats}. Reply 'suggest gifts' for recommendations",
                ],
            )
        except Exception as exc:
            logger.error("gift-giver notification failed phone=%s: %s", phone[-4:], exc)


async def record_recipient_preferences(db: AsyncSession, phone: str, args: dict) -> dict:
    """Save model-extracted preferences only for the caller's live request."""
    from app.core.config import settings

    if not settings.preference_interceptor_enabled:
        return {"error": "preference_replies_disabled"}
    pending = await db.scalar(select(PreferenceRequest).where(
        PreferenceRequest.recipient_phone.in_(phone_aliases(phone)),
        PreferenceRequest.status == "pending",
        PreferenceRequest.created_at >= datetime.now(timezone.utc) - timedelta(days=PREFERENCE_REQUEST_TTL_DAYS),
    ).limit(1))
    if not pending:
        return {"error": "no_pending_preference_request"}
    raw = (args.get("raw_response") or "").strip()
    if not raw:
        return {"error": "response_required"}
    prefs = {key: ", ".join(args[key]) for key in (
        "preferred_categories", "disliked_categories", "preferred_brands", "disliked_brands"
    ) if isinstance(args.get(key), list)}
    prefs["notes"] = raw
    return await save_recipient_preferences(db, phone, raw, request_id=pending.id, parsed_preferences=prefs)
