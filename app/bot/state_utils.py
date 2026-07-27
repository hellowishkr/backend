"""Deterministic conversation state helpers.

Persists structured per-phone flags into `ConversationState.temp_data` (JSON).
Use for short-lived "awaiting X" markers that drive deterministic flow routing
outside the LLM — e.g. `awaiting=wishlist_link` after the product gate.

Intentionally thin: read/write/clear keys. No migrations — `temp_data` is an
existing Text column. Values must be JSON-serialisable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_context import (
    GIFT_NEED_PROFILE_VERSION,
    GiftNeedProfile,
    gift_need_profile_is_recent,
    parse_gift_need_profile,
)
from app.models.conversation import ConversationState

logger = logging.getLogger("wishkr.state")

ACTIVE_GIFT_CONTEXT_KEY = "active_gift_context"

_ACTIVE_GIFT_CONTEXT_FIELDS = {
    "version",
    "recipient_name",
    "recipient_phone",
    "recipient_city",
    "relationship",
    "occasion_id",
    "occasion_type",
    "occasion_date",
    "purpose",
    "recipient_vibe",
    "interest",
    "gift_category",
    "selected_brand",
    "selected_candidate_id",
    "selected_provider_id",
    "budget_min",
    "budget_max",
    "current_step",
    "recipient_message_permission",
    "updated_at",
}

_PROTECTED_CONTEXT_FIELDS = {
    "recipient_name",
    "recipient_phone",
    "occasion_id",
    "occasion_type",
    "occasion_date",
    "purpose",
}

_VALID_PURPOSES = {"gift_for_friend", "self"}


def _parse(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _as_temp_data(state_or_temp_data: Any) -> dict[str, Any]:
    if isinstance(state_or_temp_data, dict):
        return state_or_temp_data
    if isinstance(state_or_temp_data, str):
        return _parse(state_or_temp_data)
    raw = getattr(state_or_temp_data, "temp_data", None)
    return _parse(raw)


def _clean_context_updates(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (updates or {}).items():
        if key not in _ACTIVE_GIFT_CONTEXT_FIELDS:
            continue
        if value is None:
            continue
        if isinstance(value, str):
            value = " ".join(value.strip().split())
            if not value:
                continue
        if key == "purpose" and value not in _VALID_PURPOSES:
            continue
        cleaned[key] = value
    return cleaned


def get_active_gift_context(state_or_temp_data: Any) -> dict[str, Any] | None:
    """Return active gift context from parsed temp data or ConversationState."""
    temp = _as_temp_data(state_or_temp_data)
    ctx = temp.get(ACTIVE_GIFT_CONTEXT_KEY)
    return dict(ctx) if isinstance(ctx, dict) else None


def get_gift_need_profile(state_or_temp_data: Any) -> GiftNeedProfile | None:
    """Return the typed active gift profile, preserving legacy storage."""
    return parse_gift_need_profile(get_active_gift_context(state_or_temp_data))


def merge_active_gift_context(
    existing: dict[str, Any] | None,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge gift context progressively without clobbering known recipient facts.

    Pass ``_explicit_context_change=True`` in updates to replace protected
    recipient/occasion fields when the user clearly switches recipient.
    """
    existing_ctx = dict(existing or {})
    updates_copy = dict(updates or {})
    explicit_context_change = bool(updates_copy.pop("_explicit_context_change", False))
    cleaned = _clean_context_updates(updates_copy)

    for key, value in cleaned.items():
        if (
            key in _PROTECTED_CONTEXT_FIELDS
            and key in existing_ctx
            and existing_ctx.get(key)
            and existing_ctx.get(key) != value
            and not explicit_context_change
        ):
            continue
        existing_ctx[key] = value

    existing_ctx["version"] = int(existing_ctx.get("version") or GIFT_NEED_PROFILE_VERSION)
    existing_ctx["updated_at"] = datetime.now(timezone.utc).isoformat()
    return {
        key: value
        for key, value in existing_ctx.items()
        if key in _ACTIVE_GIFT_CONTEXT_FIELDS and value is not None
    }


def set_active_gift_context(
    state_or_temp_data: Any,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge updates into temp_data['active_gift_context'] and return temp_data."""
    temp = _as_temp_data(state_or_temp_data)
    existing = get_active_gift_context(temp)
    temp[ACTIVE_GIFT_CONTEXT_KEY] = merge_active_gift_context(existing, dict(updates or {}))
    return temp


def clear_active_gift_context(state_or_temp_data: Any) -> dict[str, Any]:
    """Remove active gift context from temp data and return temp_data."""
    temp = _as_temp_data(state_or_temp_data)
    temp.pop(ACTIVE_GIFT_CONTEXT_KEY, None)
    return temp


def active_gift_context_is_recent(
    ctx: dict[str, Any] | None,
    now: datetime | None = None,
    ttl_hours: int = 24,
) -> bool:
    return gift_need_profile_is_recent(
        parse_gift_need_profile(ctx),
        now=now,
        ttl_hours=ttl_hours,
    )


def has_active_gift_context(state_or_temp_data: Any) -> bool:
    return active_gift_context_is_recent(get_active_gift_context(state_or_temp_data))


def has_active_gift_need(state_or_temp_data: Any) -> bool:
    return gift_need_profile_is_recent(get_gift_need_profile(state_or_temp_data))


async def _load_row(db: AsyncSession, phone: str) -> ConversationState | None:
    result = await db.execute(
        select(ConversationState).where(ConversationState.phone == phone)
    )
    return result.scalar_one_or_none()


async def get_state(db: AsyncSession, phone: str) -> dict[str, Any]:
    """Return the full state dict for `phone` (empty if none)."""
    row = await _load_row(db, phone)
    return _parse(row.temp_data if row else None)


async def get_awaiting(db: AsyncSession, phone: str) -> str | None:
    """Convenience — return current `awaiting` key, or None."""
    state = await get_state(db, phone)
    val = state.get("awaiting")
    return val if isinstance(val, str) else None


async def set_state(db: AsyncSession, phone: str, key: str, value: Any) -> None:
    """Merge `{key: value}` into `temp_data`. Creates the row if missing."""
    row = await _load_row(db, phone)
    data = _parse(row.temp_data if row else None)
    data[key] = value
    serialised = json.dumps(data)
    if row:
        row.temp_data = serialised
    else:
        db.add(ConversationState(phone=phone, temp_data=serialised))
    await db.flush()


async def update_active_gift_context(
    db: AsyncSession,
    phone: str,
    updates: dict[str, Any],
    *,
    sync_gift_plan: bool = True,
) -> dict[str, Any]:
    """Persist progressive active gift context updates for a phone."""
    row = await _load_row(db, phone)
    data = _parse(row.temp_data if row else None)
    data = set_active_gift_context(data, dict(updates or {}))
    serialised = json.dumps(data)
    if row:
        row.temp_data = serialised
    else:
        db.add(ConversationState(phone=phone, temp_data=serialised))
    await db.flush()
    if sync_gift_plan:
        try:
            from app.services.occasion_gift_plan import persist_active_profile_to_plan

            # The durable plan is additive to transient context. Isolate it in
            # a savepoint so an integrity/database failure cannot poison the
            # caller's outer AsyncSession transaction.
            async with db.begin_nested():
                await persist_active_profile_to_plan(
                    db,
                    phone,
                    get_gift_need_profile(data),
                )
        except Exception:
            logger.exception(
                "[%s] durable gift-plan sync failed; active context preserved",
                phone,
            )
    return data[ACTIVE_GIFT_CONTEXT_KEY]


async def remember_budget(db: AsyncSession, phone: str, text: str) -> dict[str, Any] | None:
    """Extract and persist a budget mention from `text`. No-op when no
    budget signal found. Returns the stored dict or None.

    Stored shape:
        {"min": int|None, "max": int|None, "unlimited": bool, "raw": str}

    Persistence is in `temp_data.budget`, picked up by `_build_dynamic_tail`
    and surfaced to the agent each turn so a one-shot mention ("budget
    1000-3000") survives the next turn's recommendations.
    """
    if not text:
        return None
    lowered = text.lower()
    payload: dict[str, Any] | None = None

    # Phone-shaped bare digit strings ("9359615424", "+919359615424") must
    # never be treated as a budget. The agent asks for phone numbers in a few
    # flows (send_reminder_now target, friend invite) and a 10-digit reply
    # would otherwise get parsed as ₹9.3 billion and poison the next turn's
    # context. See incident 2026-04-29.
    import re as _re
    digits_only = _re.sub(r"[\s\-+()]", "", text.strip())
    if digits_only.isdigit() and 10 <= len(digits_only) <= 13:
        return None

    # Unlimited / open-ended signal — checked first so it wins over a
    # "₹0" parse that would otherwise treat unlimited as zero.
    if any(kw in lowered for kw in ("unlimited", "no budget", "any budget", "no limit")):
        payload = {"min": None, "max": None, "unlimited": True, "raw": text[:80]}
    else:
        try:
            from app.utils.budget_parser import parse_budget
            mn, mx = parse_budget(text)
        except Exception:
            mn = mx = None
        if mn is not None or mx is not None:
            payload = {"min": mn, "max": mx, "unlimited": False, "raw": text[:80]}

    if payload is None:
        return None

    await set_state(db, phone, "budget", payload)
    state = await get_state(db, phone)
    if has_active_gift_context(state):
        await update_active_gift_context(
            db,
            phone,
            {
                "budget_min": payload.get("min"),
                "budget_max": payload.get("max"),
                "current_step": "budget_known",
            },
        )
    logger.info("[%s] budget remembered: %s", phone, payload)
    return payload


async def get_budget(db: AsyncSession, phone: str) -> dict[str, Any] | None:
    """Return the persisted budget dict, or None."""
    state = await get_state(db, phone)
    val = state.get("budget")
    return val if isinstance(val, dict) else None


async def clear_state(db: AsyncSession, phone: str, key: str) -> None:
    """Remove `key` from `temp_data`. No-op if missing."""
    row = await _load_row(db, phone)
    if not row:
        return
    data = _parse(row.temp_data)
    if key not in data:
        return
    data.pop(key)
    row.temp_data = json.dumps(data) if data else None
    await db.flush()
