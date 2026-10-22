"""ADK adapter — bridges Google ADK with our tool implementations.

Provides:
1. ADK-compatible async tool functions (for LlmAgent)
2. Tool dispatcher for direct calls
3. Context variable management for request-scoped DB/phone

Gemini-facing declarations live in schemas.py. This module owns execution,
runtime validation, and request-scoped context plumbing.

Tool-signature conventions:
- Optional arguments use ``| None = None`` — never ``= ""`` or ``= 0``. ADK
  generates Gemini function declarations from these signatures, and Gemini
  emits the empty defaults as real values when told a field exists, causing
  tools to act on garbage. ``None`` translates to an absent field in the
  schema, so the model omits it unless it has a real value.
- List/dict params stay typed (``list[dict]``, ``list[str]``). Never flatten
  to a JSON-encoded string — Gemini routinely malforms hand-encoded JSON.
"""

import logging
from contextvars import ContextVar
from typing import Any, Callable, Optional

from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.bot.tools import functions
from app.bot.tools.response_boundary import with_safe_response
from app.bot.tools.schemas import TOOL_DECLARATIONS, get_tool_spec
from app.bot.tools.validators import (
    caller_can_access_occasion,
    celebrant_for_occasion,
    contact_owned,
    friend_linked,
    invited_celebrant_friend,
    normalize_phone,
    owned_occasion,
)
from app.models import Contact

logger = logging.getLogger("wishkr.tools.adapter")

MVP_DISABLED_TOOLS = frozenset({
    "create_gift_order",
    "create_friend_gift_order",
    "create_batch_gift_orders",
    "create_checkout",
    "select_gift_candidate",
    "add_to_cart",
    "checkout_cart",
    "search_brands",
    "get_brand_details",
    "suggest_marketplace_gift_card",
    "search_vendors",
    "set_contact_city",
})

MVP_DISABLED_RESPONSE = {
    "status": "disabled",
    "error": "mvp_reminder_only",
    "user_message": (
        "I can help you remember the occasion and suggest gift ideas. "
        "For now, you can buy from your preferred store."
    ),
    "retryable": False,
}

# Context variables for request-scoped data (async-safe)
_ctx_db: ContextVar[AsyncSession | None] = ContextVar("db", default=None)
_ctx_phone: ContextVar[str | None] = ContextVar("phone", default=None)
# Accumulates produced values across tool calls within one request session.
# Populated by _track_produced_values after each successful execution.
_ctx_produced: ContextVar[dict[str, Any]] = ContextVar("produced")


def get_db() -> AsyncSession:
    """Get the request-scoped database session."""
    db = _ctx_db.get()
    if not db:
        raise RuntimeError("No database session in context")
    return db


def get_phone() -> str:
    """Get the request-scoped phone number."""
    phone = _ctx_phone.get()
    if not phone:
        raise RuntimeError("No phone number in context")
    return phone


def set_context(db: AsyncSession, phone: str) -> tuple:
    """Set request context for tool execution. Returns tokens for reset."""
    token_db = _ctx_db.set(db)
    token_phone = _ctx_phone.set(phone)
    token_produced = _ctx_produced.set({})
    return token_db, token_phone, token_produced


def reset_context(token_db, token_phone, token_produced) -> None:
    """Reset context variables after request."""
    _ctx_db.reset(token_db)
    _ctx_phone.reset(token_phone)
    _ctx_produced.reset(token_produced)


# ─────────────────────────────────────────────────────────
# FUNCTION MAPPING
# Maps tool names to their implementations
# ─────────────────────────────────────────────────────────

FUNCTION_MAP: dict[str, Callable] = {
    "get_user_profile": functions.get_user_profile,
    "register_user": functions.register_user,
    "get_order_support": functions.get_order_support,
    "set_chat_preferences": functions.set_chat_preferences,
    "record_recipient_preferences": functions.record_recipient_preferences,
    "add_occasion": functions.add_occasion,
    "list_occasions": functions.list_occasions,
    "get_my_occasions": functions.get_my_occasions,
    "get_occasion_details": functions.get_occasion_details,
    "search_brands": functions.search_brands,
    "suggest_gifts": functions.suggest_gifts,
    "save_gift_plan": functions.save_gift_plan,
    "select_gift_candidate": functions.select_gift_candidate,
    "create_checkout": functions.create_checkout,
    "suggest_marketplace_gift_card": functions.suggest_marketplace_gift_card,
    "get_self_preferences": functions.get_self_preferences,
    "get_brand_details": functions.get_brand_details,
    "create_gift_order": functions.create_gift_order,
    "list_orders": functions.list_orders,
    "check_occasion_gifts": functions.check_occasion_gifts,
    "log_external_purchase": functions.log_external_purchase,
    "log_my_gift": functions.log_my_gift,
    "add_to_wishlist": functions.add_to_wishlist,
    "list_my_wishlist": functions.list_my_wishlist,
    "remove_wishlist_item": functions.remove_wishlist_item,
    "clear_wishlist": functions.clear_wishlist,
    "get_friend_wishlist": functions.get_friend_wishlist,
    "reserve_wishlist_item": functions.reserve_wishlist_item,
    "confirm_wishlist_purchase": functions.confirm_wishlist_purchase,
    "request_recipient_preferences": functions.request_recipient_preferences,
    "get_upcoming_digest": functions.get_upcoming_digest,
    "snooze_occasion": functions.snooze_occasion,
    "skip_occasion_this_year": functions.skip_occasion_this_year,
    "get_monthly_spend": functions.get_monthly_spend,
    "create_batch_gift_orders": functions.create_batch_gift_orders,
    "setup_celebrant_occasion": functions.setup_celebrant_occasion,
    "update_celebrant_occasion": functions.update_celebrant_occasion,
    "get_celebrant_context": functions.get_celebrant_context,
    "create_friend_gift_order": functions.create_friend_gift_order,
    "edit_occasion": functions.edit_occasion,
    "delete_occasion": functions.delete_occasion,
    "delete_all_occasions": functions.delete_all_occasions,
    "merge_occasions": functions.merge_occasions,
    "add_to_cart": functions.add_to_cart,
    "view_cart": functions.view_cart,
    "checkout_cart": functions.checkout_cart,
    "clear_cart": functions.clear_cart,
    "search_vendors": functions.search_vendors,
    "set_contact_city": functions.set_contact_city,
    "get_reminder_schedule": functions.get_reminder_schedule,
    "get_reminder_history": functions.get_reminder_history,
    "set_reminder_schedule": functions.set_reminder_schedule,
    "send_reminder_now": functions.send_reminder_now,
}


def _precondition_failed(
    reason: str,
    required_action: str,
    *,
    prerequisite_tool: str | None = None,
    missing: list[str] | None = None,
) -> dict:
    payload: dict[str, Any] = {
        "error": "precondition_failed",
        "reason": reason,
        "required_action": required_action,
        "retryable": False,
    }
    if prerequisite_tool:
        payload["prerequisite_tool"] = prerequisite_tool
    if missing:
        payload["missing"] = missing
    return payload


def _invalid_input(
    reason: str,
    required_action: str,
    *,
    missing: list[str] | None = None,
    **extra: Any,
) -> dict:
    payload: dict[str, Any] = {
        "error": "invalid_input",
        "reason": reason,
        "required_action": required_action,
        "retryable": False,
    }
    if missing:
        payload["missing"] = missing
    payload.update(extra)
    return payload


def _system_error(tool_name: str, reason: str) -> dict:
    return {
        "error": "system_error",
        "tool": tool_name,
        "reason": reason[:200],
        "required_action": (
            "Do not blindly retry the same call. Apologise briefly, then choose "
            "a different valid tool path or ask the user to try again later."
        ),
        "retryable": True,
    }


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


# ─────────────────────────────────────────────────────────
# CONDITION HANDLERS
# Maps ToolSpec conditional_requires keys → predicate(args) → bool.
#
# Three semantic categories:
#   always-active   – semantic ownership/context invariants ("caller", "cart",
#                     "brand"); their requirements are always enforced.
#   response-time   – conditions that depend on the tool's own output (e.g.
#                     duplicate_warning, fixed_denomination_brand); we cannot
#                     evaluate them before execution, so we skip pre-validation.
#   handled-elsewhere – "each_item" is processed via a dedicated item loop in
#                     _validate_tool_preconditions; exclude here to avoid
#                     double-processing.
# ─────────────────────────────────────────────────────────

_ALWAYS_ACTIVE_CONDITIONS = frozenset({"caller", "cart", "brand"})
_RESPONSE_TIME_CONDITIONS = frozenset({
    "duplicate_warning",
    "fixed_denomination_brand",
    "previous_add_to_wishlist_urls",
    "confirmation",
})
_HANDLED_ELSEWHERE_CONDITIONS = frozenset({"each_item"})

CONDITION_HANDLERS: dict[str, Callable[[dict], bool]] = {
    # Field-presence conditions
    "target_phone_present": lambda args: _present(args.get("target_phone")),
    "occasion_id_present": lambda args: _present(args.get("occasion_id")),
    "celebrant_occasion_id": lambda args: _present(args.get("celebrant_occasion_id")),
    "contact_id": lambda args: _present(args.get("contact_id")),
    "contact_name": lambda args: _present(args.get("contact_name")),
    # Local-vendor results fire when occasion_id is supplied (city-scoped path).
    "local_vendor_results": lambda args: _present(args.get("occasion_id")),
    # Always-active: ownership / context invariants
    **{k: lambda args, _k=k: True for k in _ALWAYS_ACTIVE_CONDITIONS},
    # Response-time: skip pre-execution enforcement
    **{k: lambda args, _k=k: False for k in _RESPONSE_TIME_CONDITIONS},
    # Handled by a dedicated item-level loop elsewhere
    **{k: lambda args, _k=k: False for k in _HANDLED_ELSEWHERE_CONDITIONS},
}


def _condition_applies(condition: str, args: dict, *, tool_name: str | None = None) -> bool:
    # Equality check: "field=value"
    if "=" in condition:
        field, expected = condition.split("=", 1)
        if not field:
            raise RuntimeError(f"Malformed ToolSpec condition: {condition!r}")
        if tool_name is not None:
            schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}
            if field not in schema_props:
                raise RuntimeError(
                    f"Unknown ToolSpec condition field {field!r} for tool {tool_name!r}"
                )
        return str(args.get(field) or "").strip() == expected
    # Explicit handler takes precedence over generic suffix logic.
    handler = CONDITION_HANDLERS.get(condition)
    if handler is not None:
        return handler(args)
    raise RuntimeError(f"Unknown ToolSpec condition: {condition!r}")


async def _validate_brand_amount(args: dict, db: AsyncSession | None = None) -> dict | None:
    brand_name = (args.get("brand_name") or "").strip()
    amount = args.get("amount")
    if not brand_name or not amount:
        return None
    try:
        amount_int = int(amount)
    except (TypeError, ValueError):
        return _invalid_input(
            "amount must be a positive integer.",
            "Ask the user for a valid rupee amount.",
            missing=["amount"],
        )
    if amount_int <= 0:
        return _invalid_input(
            "amount must be greater than zero.",
            "Ask the user for a valid rupee amount.",
            missing=["amount"],
        )
    brand_id = await functions._find_brand_id(brand_name, db)
    if not brand_id:
        return None
    amount_error = await functions._check_brand_amount(brand_id, brand_name, amount_int)
    if amount_error:
        return {
            **amount_error,
            "error": "invalid_input",
            "reason": amount_error.get("message", "Invalid brand amount."),
            "required_action": "Show the allowed amount details and ask the user to choose a valid amount.",
            "retryable": False,
        }
    return None


# ─────────────────────────────────────────────────────────
# RUNTIME REQUIREMENT HANDLERS (Layer B)
# One async function per DB-backed check. Each handler receives the full
# call context as keyword-only args and returns a precondition_failed payload
# or None. Kept small and independently testable.
# ─────────────────────────────────────────────────────────

async def _req_recipient_occasion_id(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    if not _present(args.get("recipient_occasion_id")):
        return _precondition_failed(
            "purpose='gift' requires recipient_occasion_id.",
            "Use purpose='self' unless FriendContext/occasion context is active; "
            "otherwise ask which occasion this gift is for.",
            missing=["recipient_occasion_id"],
        )
    return None


async def _req_owned_occasion(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    if occasion_id and await owned_occasion(db, phone, occasion_id) is None:
        return _precondition_failed(
            "occasion_id is missing or not owned by the current user.",
            "Call get_my_occasions or get_occasion_details first and use an owned occasion_id.",
            prerequisite_tool="get_my_occasions",
        )
    return None


async def _req_occasion_accessible(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    if occasion_id and not await caller_can_access_occasion(db, phone, occasion_id):
        return _precondition_failed(
            "occasion_id is not accessible to the current user.",
            "Use an occasion_id returned by get_my_occasions or get_celebrant_context.",
            prerequisite_tool="get_my_occasions",
        )
    return None


async def _req_own_or_invited(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id or not await caller_can_access_occasion(db, phone, occasion_id):
        return _precondition_failed(
            "caller must own this occasion or be an invited friend.",
            "Use get_my_occasions for owner flows or get_celebrant_context for friend flows.",
            prerequisite_tool="get_celebrant_context",
        )
    return None


async def _req_occasion_has_contact(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    occasion = await owned_occasion(db, phone, occasion_id)
    if tool_name == "search_vendors":
        if occasion is None or not occasion.contact_id:
            return _precondition_failed(
                "local vendor search requires an owned friend occasion with a contact.",
                "Call get_occasion_details first; if this is an own celebrant occasion, "
                "search national vendors without occasion_id.",
                prerequisite_tool="get_occasion_details",
            )
        return None
    if occasion is not None and not occasion.contact_id:
        return _precondition_failed(
            "contact_name can only be edited for friend/contact occasions.",
            "Do not use contact_name for the user's own occasion; update only the occasion fields.",
        )
    return None


async def _req_contact_owned_via_details(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    contact_id = (args.get("contact_id") or "").strip()
    if await contact_owned(db, phone, contact_id) is None:
        return _precondition_failed(
            "contact_id is missing or not owned by the current user.",
            "Call get_occasion_details for the selected occasion, then retry with that contact_id.",
            prerequisite_tool="get_occasion_details",
        )
    return None


async def _req_contact_city(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    occasion = await owned_occasion(db, phone, occasion_id)
    contact = (
        await db.get(Contact, occasion.contact_id)
        if occasion and occasion.contact_id
        else None
    )
    if contact is None or not contact.city_id:
        return _precondition_failed(
            "local vendor search requires recipient contact city.",
            "Call get_occasion_details, ask for the contact city if missing, then call set_contact_city.",
            prerequisite_tool="set_contact_city",
        )
    return None


async def _req_celebrant_occasion_exists(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id:
        return _precondition_failed(
            "targeted reminder requires occasion_id.",
            "Call setup_celebrant_occasion first or ask the user which occasion to use.",
            prerequisite_tool="setup_celebrant_occasion",
            missing=["occasion_id"],
        )
    if await owned_occasion(db, phone, occasion_id) is None:
        return _precondition_failed(
            "targeted reminder requires a friend-setup occasion the caller owns.",
            "Call setup_celebrant_occasion first and use the occasion_id it returns.",
            prerequisite_tool="setup_celebrant_occasion",
        )
    cel_occ = await celebrant_for_occasion(db, occasion_id)
    if cel_occ is None:
        return _precondition_failed(
            "target_phone requires a friend-setup row for this occasion.",
            "Call setup_celebrant_occasion first, then retry send_reminder_now with the returned occasion_id.",
            prerequisite_tool="setup_celebrant_occasion",
        )
    return None


async def _req_friend_linked(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    occasion_id = (args.get("occasion_id") or "").strip()
    target_phone = normalize_phone((args.get("target_phone") or "").strip())
    cel_occ = await celebrant_for_occasion(db, occasion_id)
    if cel_occ is not None and not await friend_linked(db, cel_occ, target_phone):
        return _precondition_failed(
            "target_phone is not linked to this friend-setup occasion.",
            "Call setup_celebrant_occasion with this friend phone before sending a targeted reminder.",
            prerequisite_tool="setup_celebrant_occasion",
        )
    return None


async def _req_invited_celebrant_friend(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    cel_occ_id = (args.get("celebrant_occasion_id") or "").strip()
    if not cel_occ_id:
        return _precondition_failed(
            "friend flow requires a friend context id from a reminder link.",
            "Ask the user to open the original reminder link, then call get_celebrant_context.",
            prerequisite_tool="get_celebrant_context",
            missing=["celebrant_occasion_id"],
        )
    cel_occ, invited = await invited_celebrant_friend(db, phone, cel_occ_id)
    if cel_occ is None:
        # The celebrant occasion behind this link is gone (owner removed it).
        # Re-opening the link yields the same dead id, so never tell the user
        # to retry it — that traps the friend in a loop. Pivot to helping them
        # directly instead.
        return _precondition_failed(
            "this reminder is no longer available — the celebrant removed the occasion.",
            "Do not mention links or ask the user to re-open anything. Briefly say "
            "this reminder is no longer active, then offer to help them directly — "
            "gift ideas, or setting up reminders for their own occasions.",
        )
    if not invited:
        return _precondition_failed(
            "caller is not invited for this friend-setup occasion.",
            "Do not retry. Ask the user to open the original reminder link.",
        )
    return None


async def _req_valid_denomination(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    return await _validate_brand_amount(args, db)


async def _req_cart_nonempty(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    cart, _temp, _state, expired = await functions._load_active_cart(db, phone)
    if not cart:
        if expired:
            return _precondition_failed(
                "cart expired after 24 hours.",
                "Tell the user the cart expired and ask them to add items again.",
                prerequisite_tool="add_to_cart",
            )
        return _precondition_failed(
            "cart is empty.",
            "Ask the user to add items before checkout.",
            prerequisite_tool="add_to_cart",
        )
    return None


async def _req_noop(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    # Informational requirement; cannot be enforced before the tool executes.
    return None


async def _req_purpose_matches_active_context(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    """Reject wishlist writes that contradict an active gift-for-friend flow.

    When ``active_gift_context.purpose == "gift_for_friend"`` and the caller
    submits ``purpose != "gift"`` (including the empty/omitted case that
    would otherwise silently default to ``"self"`` in the tool impl), fail
    the call rather than corrupt the caller's personal wishlist.

    See the Swaraj/BGMI post-mortem, step 8.
    """
    # Local import — avoids a top-level import cycle and mirrors how
    # agent.py already reaches into state_utils.
    from app.bot.state_utils import (
        active_gift_context_is_recent,
        get_active_gift_context,
        get_state,
    )

    state = await get_state(db, phone)
    ctx = get_active_gift_context(state)
    if not active_gift_context_is_recent(ctx):
        return None
    if (ctx or {}).get("purpose") != "gift_for_friend":
        return None

    incoming_purpose = (args.get("purpose") or "").strip().lower()
    if incoming_purpose == "gift":
        return None

    logger.info(
        "[%s] purpose_mismatch_active_context tool=%s incoming_purpose=%r",
        phone, tool_name, incoming_purpose or None,
    )
    payload = _precondition_failed(
        "purpose='gift' is required while a gift-for-friend flow is active.",
        "Pass purpose='gift' with the active recipient_occasion_id, or clear "
        "the gift context if the user actually wants this item for themselves.",
        missing=["purpose"],
    )
    payload["user_message"] = (
        "I need to know which friend's occasion this gift is for."
    )
    payload["internal_reason"] = "purpose_mismatch_active_context"
    return payload


async def _req_recipient_message_permission(
    *, tool_name: str, args: dict, db: AsyncSession, phone: str
) -> dict | None:
    """Block outbound recipient messages unless permission is explicit.

    The model may pass ``permission_confirmed=True`` only after the gift-giver
    explicitly agrees in the conversation. Future flows can also persist
    ``recipient_message_permission`` in the active gift context.
    """
    if bool(args.get("permission_confirmed")):
        return None
    from app.bot.state_utils import get_gift_need_profile, get_state

    profile = get_gift_need_profile(await get_state(db, phone))
    if profile and profile.is_recent() and profile.recipient_message_permission is True:
        return None

    payload = _precondition_failed(
        "recipient messaging requires explicit gift-giver permission.",
        "Ask the gift-giver for permission to message the recipient, then retry with permission_confirmed=true.",
        missing=["permission_confirmed"],
    )
    payload["user_message"] = (
        "Please confirm that I should message the recipient for their gift preferences."
    )
    payload["internal_reason"] = "recipient_message_permission_missing"
    return payload


# ─────────────────────────────────────────────────────────
# RUNTIME_CHECKS
# Maps ToolSpec requirement keys → async handler functions.
# Adding a new DB-backed requirement requires only an entry here.
# ─────────────────────────────────────────────────────────

RuntimeCheckFn = Callable[..., Any]  # async (*, tool_name, args, db, phone) -> dict | None

RUNTIME_CHECKS: dict[str, RuntimeCheckFn] = {
    # Occasion ownership / access
    "occasion_owned":                       _req_owned_occasion,
    "occasion_owned_if_present":            _req_owned_occasion,
    "occasion_accessible":                  _req_occasion_accessible,
    "occasion_accessible_to_caller":        _req_own_or_invited,
    "occasion_has_contact":                 _req_occasion_has_contact,
    # Contact ownership
    "contact_owned":                        _req_contact_owned_via_details,
    "contact_city_set":                     _req_contact_city,
    # Wishlist purpose
    "recipient_occasion_id_present":        _req_recipient_occasion_id,
    "purpose_matches_active_context":       _req_purpose_matches_active_context,
    "recipient_message_permission":         _req_recipient_message_permission,
    # Celebrant / friend flow
    "celebrant_occasion_exists":            _req_celebrant_occasion_exists,
    "target_phone_linked_to_friend":        _req_friend_linked,
    "celebrant_friend_invited":             _req_invited_celebrant_friend,
    # Denomination / cart
    "valid_denomination":                   _req_valid_denomination,
    "cart_nonempty_unexpired":              _req_cart_nonempty,
    # Informational / response-time (cannot enforce pre-execution)
    "self_preferences_checked":             _req_noop,
    "duplicate_acknowledged_after_confirmation": _req_noop,
    "wishlist_urls_passed_explicitly":       _req_noop,
    "delete_confirmed":                     _req_noop,
}


async def _check_runtime_requirement(
    requirement: str,
    *,
    tool_name: str,
    args: dict,
    db: AsyncSession,
    phone: str,
) -> dict | None:
    """Two-layer validation for a single ToolSpec requirement string.

    Layer A — Generic field-presence check (no DB access):
        If the requirement is a field name declared in the tool's schema AND has
        no dedicated runtime handler, validate that the caller supplied a value.

    Layer B — DB-backed runtime check:
        Look up the requirement in RUNTIME_CHECKS and delegate to its handler.
        All ownership, relationship, and state checks live here.
    """
    # Layer A: generic field-presence — only when no explicit handler exists.
    if requirement not in RUNTIME_CHECKS:
        schema_props = (TOOL_DECLARATIONS.get(tool_name).parameters.properties or {})
        if requirement in schema_props:
            if not _present(args.get(requirement)):
                return _precondition_failed(
                    f"{tool_name} requires {requirement} for this tool path.",
                    f"Ask the user only for the missing input: {requirement}.",
                    missing=[requirement],
                )
            return None

    # Layer B: dispatch to the named runtime handler.
    handler = RUNTIME_CHECKS.get(requirement)
    if handler:
        return await handler(tool_name=tool_name, args=args, db=db, phone=phone)

    raise RuntimeError(
        f"Unhandled ToolSpec requirement {requirement!r} for tool {tool_name!r}"
    )


def _track_produced_values(tool_name: str, result: dict) -> None:
    """Store values produced by a successful tool call into the request-scoped context.

    Retained for future use (e.g. audit logging, cross-tool data tracing).
    Does NOT bypass required-field validation — required fields must always
    be present in the tool call args supplied by the model.
    """
    spec = get_tool_spec(tool_name)
    produced_keys = spec.produces
    if not produced_keys:
        return
    current = dict(_ctx_produced.get({}))
    for key in produced_keys:
        if key in result and _present(result[key]):
            current[key] = result[key]
    _ctx_produced.set(current)


async def _validate_tool_preconditions(
    tool_name: str,
    args: dict,
    db: AsyncSession,
    phone: str,
) -> dict | None:
    """Block known-invalid tool calls before they reach implementation code.

    Prompt instructions reduce bad calls; this guard makes high-risk
    dependencies deterministic and returns structured repair instructions
    instead of laundering the issue into a generic system failure.
    """
    spec = get_tool_spec(tool_name)
    schema_props = TOOL_DECLARATIONS[tool_name].parameters.properties or {}

    for field in spec.requires:
        if field not in schema_props:
            raise RuntimeError(
                f"Unknown ToolSpec required field {field!r} for tool {tool_name!r}"
            )

    missing_required = [
        field
        for field in spec.requires
        if not _present(args.get(field))
    ]
    if missing_required:
        return _precondition_failed(
            f"Missing required field(s): {', '.join(missing_required)}.",
            f"Ask the user only for the missing input(s): {', '.join(missing_required)}.",
            missing=missing_required,
        )

    for condition, requirements in spec.conditional_requires.items():
        if not _condition_applies(condition, args, tool_name=tool_name):
            continue
        for requirement in requirements:
            error = await _check_runtime_requirement(
                requirement,
                tool_name=tool_name,
                args=args,
                db=db,
                phone=phone,
            )
            if error:
                return error

    # Direct amount validation for single-order and cart staging paths. This
    # uses the same helper as create_gift_order, but runs before any order,
    # cart, or payment-link mutation.
    if tool_name in {"create_gift_order", "create_friend_gift_order", "add_to_cart"}:
        amount_error = await _validate_brand_amount(args, db)
        if amount_error:
            return amount_error

    if tool_name == "create_batch_gift_orders":
        for item in args.get("items") or []:
            amount_error = await _validate_brand_amount(item, db)
            if amount_error:
                amount_error["item"] = item
                return amount_error
            if _present(item.get("occasion_id")):
                ownership_error = await _check_runtime_requirement(
                    "occasion_owned_if_present",
                    tool_name=tool_name,
                    args=item,
                    db=db,
                    phone=phone,
                )
                if ownership_error:
                    ownership_error["item"] = item
                    return ownership_error

    return None


async def execute_tool(tool_name: str, args: dict) -> dict:
    """Execute a tool by name with given arguments.

    Uses context variables for db/phone — set them before calling.

    Error envelope: returns ``{"error": "<code>", "required_action": "<actionable>"}``
    rather than a free-form string. Gemini self-corrects on structured
    errors and confabulates on generic failures.
    """
    handler = FUNCTION_MAP.get(tool_name)
    if not handler:
        return with_safe_response(
            _invalid_input(
                f"Unknown tool: {tool_name}.",
                "Pick from the declared tool set; do not invent tool names.",
                tool=tool_name,
            )
        )

    if settings.mvp_reminder_only and tool_name in MVP_DISABLED_TOOLS:
        logger.info("Tool %s disabled by MVP_REMINDER_ONLY", tool_name)
        return with_safe_response(dict(MVP_DISABLED_RESPONSE))

    db: AsyncSession = get_db()
    phone = get_phone()

    try:
        precondition_error = await _validate_tool_preconditions(tool_name, args, db, phone)
        if precondition_error:
            logger.info("Tool %s precondition failed: %s", tool_name, precondition_error)
            return with_safe_response(precondition_error)

        result = await handler(db, phone, args)
        if "error" not in result:
            _track_produced_values(tool_name, result)
        logger.info("Tool %s executed: %s", tool_name, str(result)[:200])
        return with_safe_response(result)
    except Exception as e:
        logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback after tool %s failure also failed", tool_name)
        # Ship to Sentry so tool crashes like the `budget_label` one
        # surface as real alerts instead of being laundered into a
        # generic user reply. Tool name goes into a tag so Sentry groups
        # by tool automatically.
        try:
            import sentry_sdk
            from app.core.observability import capture_exception as _sentry_capture
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("tool", tool_name)
                scope.set_tag("error_class", "system_error")
                _sentry_capture(e, tool=tool_name, args_preview=str(args)[:200])
        except Exception:
            pass
        return with_safe_response(_system_error(tool_name, str(e)))


# ─────────────────────────────────────────────────────────
# ADK TOOL WRAPPERS
# Async functions that ADK can call directly.
#
# Convention: optional args are ``| None = None``. We only inject them into
# the dispatcher's args dict when they carry real values, so downstream
# functions.py handlers never see sentinel empties.
# ─────────────────────────────────────────────────────────

async def get_user_profile() -> dict:
    """ADK wrapper for get_user_profile."""
    return await execute_tool("get_user_profile", {})


async def register_user(
    name: str,
    city: str | None = None,
    timezone: str | None = None,
) -> dict:
    """ADK wrapper for register_user.

    Optional city is forwarded only when present.
    """
    args: dict[str, Any] = {"name": name}
    if city:
        args["city"] = city
    if timezone:
        args["timezone"] = timezone
    return await execute_tool("register_user", args)


async def add_occasion(
    contact_name: str,
    occasion_type: str,
    date_day: int,
    date_month: int,
    date_year: int | None = None,
    date_input_numeric: bool | None = None,
    date_confirmed: bool | None = None,
    recurring: bool | None = None,
    custom_label: str | None = None,
    relationship: str | None = None,
    contact_phone: str | None = None,
    recipient_city: str | None = None,
) -> dict:
    """ADK wrapper for add_occasion.

    Optional fields are forwarded only when present.
    """
    args: dict[str, Any] = {
        "contact_name": contact_name,
        "occasion_type": occasion_type,
        "date_day": date_day,
        "date_month": date_month,
    }
    if custom_label:
        args["custom_label"] = custom_label
    if date_year is not None:
        args["date_year"] = date_year
    if date_input_numeric is not None:
        args["date_input_numeric"] = date_input_numeric
    if date_confirmed is not None:
        args["date_confirmed"] = date_confirmed
    if recurring is not None:
        args["recurring"] = recurring
    if relationship:
        args["relationship"] = relationship
    if contact_phone:
        args["contact_phone"] = contact_phone
    if recipient_city:
        args["recipient_city"] = recipient_city
    return await execute_tool("add_occasion", args)


async def list_occasions() -> dict:
    """ADK wrapper for list_occasions."""
    return await execute_tool("list_occasions", {})


async def get_my_occasions() -> dict:
    """ADK wrapper for get_my_occasions."""
    return await execute_tool("get_my_occasions", {})


async def get_occasion_details(occasion_id: str) -> dict:
    """ADK wrapper for get_occasion_details."""
    return await execute_tool("get_occasion_details", {"occasion_id": occasion_id})


async def search_brands(query: str | None = None, budget_max: int | None = None) -> dict:
    """ADK wrapper for search_brands.

    Query and budget filters are forwarded only when useful.
    """
    args: dict[str, Any] = {}
    if query:
        args["query"] = query
    if budget_max is not None and budget_max > 0:
        args["budget_max"] = budget_max
    return await execute_tool("search_brands", args)


async def get_brand_details(brand_name: str) -> dict:
    """ADK wrapper for get_brand_details."""
    return await execute_tool("get_brand_details", {"brand_name": brand_name})


async def create_gift_order(
    brand_name: str,
    amount: int,
    occasion_id: str | None = None,
    wishlist_item_id: str | None = None,
    duplicate_acknowledged: bool = False,
) -> dict:
    """ADK wrapper for create_gift_order.

    Argument filtering stays here; tool contract lives in schemas.py.
    """
    args: dict[str, Any] = {"brand_name": brand_name, "amount": amount}
    if occasion_id:
        args["occasion_id"] = occasion_id
    if wishlist_item_id:
        args["wishlist_item_id"] = wishlist_item_id
    if duplicate_acknowledged:
        args["duplicate_acknowledged"] = True
    return await execute_tool("create_gift_order", args)


async def list_orders() -> dict:
    """ADK wrapper for list_orders."""
    return await execute_tool("list_orders", {})


async def check_occasion_gifts(occasion_id: str) -> dict:
    """ADK wrapper for check_occasion_gifts."""
    return await execute_tool("check_occasion_gifts", {"occasion_id": occasion_id})


async def log_external_purchase(
    occasion_id: str,
    item_description: str,
    category: str | None = None,
    brand_name: str | None = None,
    wishlist_item_id: str | None = None,
) -> dict:
    """ADK wrapper for log_external_purchase.

    Optional metadata is forwarded only when present.
    """
    args: dict[str, Any] = {"occasion_id": occasion_id, "item_description": item_description}
    if category:
        args["category"] = category
    if brand_name:
        args["brand_name"] = brand_name
    if wishlist_item_id:
        args["wishlist_item_id"] = wishlist_item_id
    return await execute_tool("log_external_purchase", args)


async def log_my_gift(
    description: str,
    recipient_name: str | None = None,
    contact_id: str | None = None,
    occasion_id: str | None = None,
    gift_year: str | None = None,
) -> dict:
    """ADK wrapper for log_my_gift.

    Records a gift the user gave someone (off-platform) so future suggestions
    can avoid repeats. Optional fields forwarded only when present.
    """
    args: dict[str, Any] = {"description": description}
    if recipient_name:
        args["recipient_name"] = recipient_name
    if contact_id:
        args["contact_id"] = contact_id
    if occasion_id:
        args["occasion_id"] = occasion_id
    if gift_year:
        args["gift_year"] = gift_year
    return await execute_tool("log_my_gift", args)


async def add_to_wishlist(
    url: str,
    purpose: str | None = None,
    recipient_occasion_id: str | None = None,
    own_occasion_id: str | None = None,
    visibility: str | None = None,
    remind_on: str | None = None,
) -> dict:
    """ADK wrapper for add_to_wishlist.

    Optional purpose/context/reminder fields are forwarded only when present.
    """
    args: dict[str, Any] = {"url": url}
    if purpose:
        args["purpose"] = purpose
    if recipient_occasion_id:
        args["recipient_occasion_id"] = recipient_occasion_id
    if own_occasion_id:
        args["own_occasion_id"] = own_occasion_id
    if visibility:
        args["visibility"] = visibility
    if remind_on:
        args["remind_on"] = remind_on
    return await execute_tool("add_to_wishlist", args)


async def list_my_wishlist(own_occasion_id: str | None = None) -> dict:
    """ADK wrapper for list_my_wishlist."""
    args: dict[str, Any] = {}
    if own_occasion_id is not None:
        args["own_occasion_id"] = own_occasion_id
    return await execute_tool("list_my_wishlist", args)


async def remove_wishlist_item(url: str) -> dict:
    """ADK wrapper for remove_wishlist_item."""
    return await execute_tool("remove_wishlist_item", {"url": url})


async def clear_wishlist() -> dict:
    """ADK wrapper for clear_wishlist."""
    return await execute_tool("clear_wishlist", {})


async def get_friend_wishlist(celebrant_occasion_id: str) -> dict:
    """ADK wrapper for get_friend_wishlist."""
    from app.core.config import settings
    if not settings.enable_shared_wishlist:
        return {
            "status": "disabled",
            "message": "Wishlist sharing is not enabled yet.",
        }
    return await execute_tool("get_friend_wishlist", {"celebrant_occasion_id": celebrant_occasion_id})


async def reserve_wishlist_item(wishlist_item_id: str, celebrant_occasion_id: str) -> dict:
    """ADK wrapper for reserve_wishlist_item."""
    from app.core.config import settings
    if not settings.enable_shared_wishlist:
        return {
            "status": "disabled",
            "message": "Wishlist sharing is not enabled yet.",
        }
    return await execute_tool("reserve_wishlist_item", {
        "wishlist_item_id": wishlist_item_id,
        "celebrant_occasion_id": celebrant_occasion_id,
    })


async def confirm_wishlist_purchase(wishlist_item_id: str, celebrant_occasion_id: str) -> dict:
    """ADK wrapper for confirm_wishlist_purchase."""
    from app.core.config import settings
    if not settings.enable_shared_wishlist:
        return {
            "status": "disabled",
            "message": "Wishlist sharing is not enabled yet.",
        }
    return await execute_tool("confirm_wishlist_purchase", {
        "wishlist_item_id": wishlist_item_id,
        "celebrant_occasion_id": celebrant_occasion_id,
    })


async def request_recipient_preferences(
    recipient_phone: str,
    occasion_id: str | None = None,
    anonymous: bool = True,
    permission_confirmed: bool = False,
) -> dict:
    """ADK wrapper for request_recipient_preferences."""
    from app.core.config import settings
    if not settings.preference_outbound_enabled:
        return {
            "status": "disabled",
            "message": "Preference request messaging is not enabled yet.",
        }
    args: dict[str, Any] = {"recipient_phone": recipient_phone, "anonymous": anonymous}
    if occasion_id:
        args["occasion_id"] = occasion_id
    if permission_confirmed:
        args["permission_confirmed"] = True
    return await execute_tool("request_recipient_preferences", args)


async def get_upcoming_digest(days: int = 7, include_own: bool = False) -> dict:
    """ADK wrapper for get_upcoming_digest."""
    return await execute_tool("get_upcoming_digest", {"days": days, "include_own": include_own})


async def snooze_occasion(occasion_id: str, days: int = 2) -> dict:
    """ADK wrapper for snooze_occasion."""
    return await execute_tool("snooze_occasion", {"occasion_id": occasion_id, "days": days})


async def skip_occasion_this_year(occasion_id: str) -> dict:
    """ADK wrapper for skip_occasion_this_year."""
    return await execute_tool("skip_occasion_this_year", {"occasion_id": occasion_id})


async def get_monthly_spend() -> dict:
    """ADK wrapper for get_monthly_spend."""
    return await execute_tool("get_monthly_spend", {})


async def create_batch_gift_orders(items: list[dict]) -> dict:
    """ADK wrapper for create_batch_gift_orders.

    Keeps the adapter-level list guard because ADK/model callers can still
    hand us malformed values before dispatch.
    """
    if not isinstance(items, list):
        return {
            "error": "invalid_items",
            "hint": "`items` must be a JSON array of order objects, not a string.",
        }
    return await execute_tool("create_batch_gift_orders", {"items": items})


async def setup_celebrant_occasion(
    occasion_type: str,
    date_day: int,
    date_month: int,
    date_year: int | None = None,
    recurring: bool | None = None,
    preferences: list[str] | None = None,
    friend_phones: list[str] | None = None,
    custom_label: str | None = None,
    wishlist_urls: list[dict] | None = None,
    note_to_friends: str | None = None,
) -> dict:
    """ADK wrapper for setup_celebrant_occasion.

    List fields are normalized to empty lists; optional fields are forwarded
    only when present.
    """
    args: dict[str, Any] = {
        "occasion_type": occasion_type,
        "date_day": date_day,
        "date_month": date_month,
    }
    if date_year is not None:
        args["date_year"] = date_year
    if recurring is not None:
        args["recurring"] = recurring
    if preferences is not None:
        args["preferences"] = preferences
    if friend_phones is not None:
        args["friend_phones"] = friend_phones
    if custom_label:
        args["custom_label"] = custom_label
    if wishlist_urls is not None:
        args["wishlist_urls"] = wishlist_urls
    if note_to_friends is not None:
        args["note_to_friends"] = note_to_friends
    return await execute_tool("setup_celebrant_occasion", args)


async def update_celebrant_occasion(
    occasion_id: str,
    occasion_type: str | None = None,
    date_day: int | None = None,
    date_month: int | None = None,
    date_year: int | None = None,
    recurring: bool | None = None,
    custom_label: str | None = None,
    preferences: list[str] | None = None,
    wishlist_urls: list[dict] | None = None,
    note_to_friends: str | None = None,
    add_friend_phones: list[str] | None = None,
    remove_friend_phones: list[str] | None = None,
) -> dict:
    """Progressively edit an existing celebrant occasion."""
    args: dict[str, Any] = {"occasion_id": occasion_id}
    optional = {
        "occasion_type": occasion_type,
        "date_day": date_day,
        "date_month": date_month,
        "date_year": date_year,
        "recurring": recurring,
        "custom_label": custom_label,
        "preferences": preferences,
        "wishlist_urls": wishlist_urls,
        "note_to_friends": note_to_friends,
        "add_friend_phones": add_friend_phones,
        "remove_friend_phones": remove_friend_phones,
    }
    args.update({key: value for key, value in optional.items() if value is not None})
    return await execute_tool("update_celebrant_occasion", args)


async def get_celebrant_context(celebrant_occasion_id: str) -> dict:
    """ADK wrapper for get_celebrant_context."""
    return await execute_tool("get_celebrant_context", {"celebrant_occasion_id": celebrant_occasion_id})


async def create_friend_gift_order(
    celebrant_occasion_id: str,
    brand_name: str,
    amount: int,
    buyer_name: str,
    personal_message: str | None = None,
    wishlist_item_id: str | None = None,
    duplicate_acknowledged: bool = False,
) -> dict:
    """ADK wrapper for create_friend_gift_order.

    Argument filtering stays here; tool contract lives in schemas.py.
    """
    args: dict[str, Any] = {
        "celebrant_occasion_id": celebrant_occasion_id,
        "brand_name": brand_name,
        "amount": amount,
        "buyer_name": buyer_name,
    }
    if personal_message:
        args["personal_message"] = personal_message
    if wishlist_item_id:
        args["wishlist_item_id"] = wishlist_item_id
    if duplicate_acknowledged:
        args["duplicate_acknowledged"] = True
    return await execute_tool("create_friend_gift_order", args)


async def edit_occasion(
    occasion_id: str,
    date_day: int | None = None,
    date_month: int | None = None,
    occasion_type: str | None = None,
    custom_label: str | None = None,
    contact_name: str | None = None,
) -> dict:
    """ADK wrapper for edit_occasion.

    Only changed fields are forwarded to the dispatcher.
    """
    args: dict[str, Any] = {"occasion_id": occasion_id}
    if date_day is not None and date_day > 0:
        args["date_day"] = date_day
    if date_month is not None and date_month > 0:
        args["date_month"] = date_month
    if occasion_type:
        args["occasion_type"] = occasion_type
    if custom_label:
        args["custom_label"] = custom_label
    if contact_name:
        args["contact_name"] = contact_name
    return await execute_tool("edit_occasion", args)


async def delete_occasion(occasion_id: str) -> dict:
    """ADK wrapper for delete_occasion."""
    return await execute_tool("delete_occasion", {"occasion_id": occasion_id})


async def delete_all_occasions(confirmed: bool = False) -> dict:
    """ADK wrapper for delete_all_occasions."""
    return await execute_tool("delete_all_occasions", {"confirmed": confirmed})


async def merge_occasions(
    keep_occasion_id: str,
    merge_occasion_id: str,
    contact_name: str | None = None,
) -> dict:
    """ADK wrapper for merge_occasions."""
    args: dict[str, Any] = {
        "keep_occasion_id": keep_occasion_id,
        "merge_occasion_id": merge_occasion_id,
    }
    if contact_name:
        args["contact_name"] = contact_name
    return await execute_tool("merge_occasions", args)


async def add_to_cart(
    brand_name: str,
    amount: int,
    occasion_id: str | None = None,
) -> dict:
    """ADK wrapper for add_to_cart.

    Optional occasion context is forwarded only when present.
    """
    args: dict[str, Any] = {"brand_name": brand_name, "amount": amount}
    if occasion_id:
        args["occasion_id"] = occasion_id
    return await execute_tool("add_to_cart", args)


async def view_cart() -> dict:
    """ADK wrapper for view_cart."""
    return await execute_tool("view_cart", {})


async def checkout_cart() -> dict:
    """ADK wrapper for checkout_cart."""
    return await execute_tool("checkout_cart", {})


async def clear_cart() -> dict:
    """ADK wrapper for clear_cart."""
    return await execute_tool("clear_cart", {})


async def search_vendors(
    category: str,
    occasion_id: str | None = None,
) -> dict:
    """ADK wrapper for search_vendors."""
    args: dict[str, Any] = {"category": category}
    if occasion_id:
        args["occasion_id"] = occasion_id
    return await execute_tool("search_vendors", args)


async def get_order_support(order_id: str | None = None) -> dict:
    return await execute_tool("get_order_support", {"order_id": order_id} if order_id else {})


async def set_chat_preferences(language: str | None = None, end_conversation: bool = False) -> dict:
    return await execute_tool("set_chat_preferences", {"language": language, "end_conversation": end_conversation})


async def record_recipient_preferences(
    raw_response: str, preferred_categories: list[str] | None = None,
    disliked_categories: list[str] | None = None, preferred_brands: list[str] | None = None,
    disliked_brands: list[str] | None = None,
) -> dict:
    args = {"raw_response": raw_response}
    args.update({key: value for key, value in (
        ("preferred_categories", preferred_categories), ("disliked_categories", disliked_categories),
        ("preferred_brands", preferred_brands), ("disliked_brands", disliked_brands)
    ) if value is not None})
    return await execute_tool("record_recipient_preferences", args)


async def suggest_gifts(
    scenario: str = "for_friend",
    occasion_id: str | None = None,
    recipient_vibe: str | None = None,
    vibes: list[str] | None = None,
    budget_min: int | None = None,
    budget_max: int | None = None,
    budget_text: str | None = None,
    budget_unlimited: bool | None = None,
    n: int = 8,
    desired_categories: list[str] | None = None,
    excluded_categories: list[str] | None = None,
    gift_format: str | None = None,
    constraints: dict[str, Any] | None = None,
) -> dict:
    """ADK wrapper for suggest_gifts.

    Optional recommendation filters are forwarded only when present.
    Gemini-facing usage guidance lives in schemas.py.
    """
    args: dict[str, Any] = {"scenario": scenario, "n": n}
    if occasion_id:
        args["occasion_id"] = occasion_id
    if recipient_vibe:
        args["recipient_vibe"] = recipient_vibe
    if vibes:
        args["vibes"] = vibes
    if budget_min is not None and budget_min > 0:
        args["budget_min"] = budget_min
    if budget_max is not None and budget_max > 0:
        args["budget_max"] = budget_max
    if budget_text:
        args["budget_text"] = budget_text
    if budget_unlimited is not None:
        args["budget_unlimited"] = budget_unlimited
    for key, value in (("desired_categories", desired_categories),
                       ("excluded_categories", excluded_categories),
                       ("gift_format", gift_format), ("constraints", constraints)):
        if value is not None:
            args[key] = value
    return await execute_tool("suggest_gifts", args)


async def save_gift_plan(
    occasion_id: str,
    budget_min: int | None = None,
    budget_max: int | None = None,
    currency: str | None = None,
    interests: list[str] | None = None,
    vibe_ids: list[str] | None = None,
    desired_categories: list[str] | None = None,
    excluded_categories: list[str] | None = None,
    preferred_brands: list[str] | None = None,
    disliked_brands: list[str] | None = None,
    gift_format: str | None = None,
    constraints: dict[str, Any] | None = None,
    recipient_city: str | None = None,
) -> dict:
    """Save progressive, giver-private planning fields for one occasion."""
    args: dict[str, Any] = {"occasion_id": occasion_id}
    optional = {
        "budget_min": budget_min,
        "budget_max": budget_max,
        "currency": currency,
        "interests": interests,
        "vibe_ids": vibe_ids,
        "desired_categories": desired_categories,
        "excluded_categories": excluded_categories,
        "preferred_brands": preferred_brands,
        "disliked_brands": disliked_brands,
        "gift_format": gift_format,
        "constraints": constraints,
        "recipient_city": recipient_city,
    }
    args.update({key: value for key, value in optional.items() if value is not None})
    return await execute_tool("save_gift_plan", args)


async def suggest_marketplace_gift_card(marketplace: str, n: int = 3) -> dict:
    """ADK wrapper for suggest_marketplace_gift_card."""
    return await execute_tool("suggest_marketplace_gift_card", {"marketplace": marketplace, "n": n})


async def select_gift_candidate(provider_id: str, candidate_id: str) -> dict:
    """ADK wrapper for select_gift_candidate."""
    return await execute_tool(
        "select_gift_candidate",
        {"provider_id": provider_id, "candidate_id": candidate_id},
    )


async def create_checkout(
    amount: int,
    provider_id: str | None = None,
    candidate_id: str | None = None,
    occasion_id: str | None = None,
    celebrant_occasion_id: str | None = None,
    buyer_name: str | None = None,
    wishlist_item_id: str | None = None,
    special_instructions: str | None = None,
    duplicate_acknowledged: bool = False,
) -> dict:
    """ADK wrapper for provider-neutral checkout."""
    args: dict[str, Any] = {"amount": amount}
    if provider_id:
        args["provider_id"] = provider_id
    if candidate_id:
        args["candidate_id"] = candidate_id
    if occasion_id:
        args["occasion_id"] = occasion_id
    if celebrant_occasion_id:
        args["celebrant_occasion_id"] = celebrant_occasion_id
    if buyer_name:
        args["buyer_name"] = buyer_name
    if wishlist_item_id:
        args["wishlist_item_id"] = wishlist_item_id
    if special_instructions:
        args["special_instructions"] = special_instructions
    if duplicate_acknowledged:
        args["duplicate_acknowledged"] = True
    return await execute_tool("create_checkout", args)


async def get_self_preferences() -> dict:
    """ADK wrapper for get_self_preferences."""
    return await execute_tool("get_self_preferences", {})


async def set_contact_city(contact_id: str, city: str) -> dict:
    """ADK wrapper for set_contact_city."""
    return await execute_tool("set_contact_city", {"contact_id": contact_id, "city": city})


# ── Reminder introspection + ad-hoc dispatch (issue #009 G1/G3) ──

async def get_reminder_schedule(occasion_id: str) -> dict:
    """ADK wrapper for get_reminder_schedule."""
    return await execute_tool("get_reminder_schedule", {"occasion_id": occasion_id})


async def get_reminder_history(occasion_id: str) -> dict:
    """ADK wrapper for get_reminder_history."""
    return await execute_tool("get_reminder_history", {"occasion_id": occasion_id})


async def set_reminder_schedule(
    occasion_id: str, days_before: list[int],
    local_hour: int | None = None, local_minute: int | None = None,
) -> dict:
    """ADK wrapper for replacing an occasion's pending reminder schedule."""
    args: dict[str, Any] = {"occasion_id": occasion_id, "days_before": days_before}
    if local_hour is not None:
        args["local_hour"] = local_hour
    if local_minute is not None:
        args["local_minute"] = local_minute
    return await execute_tool("set_reminder_schedule", args)


async def send_reminder_now(occasion_id: str, target_phone: str | None = None) -> dict:
    """ADK wrapper for send_reminder_now.

    Optional target_phone is forwarded only when present.
    """
    args: dict[str, Any] = {"occasion_id": occasion_id}
    if target_phone:
        args["target_phone"] = target_phone
    return await execute_tool("send_reminder_now", args)


# ─────────────────────────────────────────────────────────
# SCHEMA-DRIVEN FUNCTION TOOL
# The wrappers above still provide execution. The schema Gemini sees is
# hand-written in schemas.py — ADK's auto-derived declaration is overridden.
# Signatures remain for IDE/static-analysis benefit; Gemini sees schemas.py.
# ─────────────────────────────────────────────────────────


class SchemaFunctionTool(FunctionTool):
    """FunctionTool that returns a hand-written FunctionDeclaration.

    ADK's default ``FunctionTool._get_declaration`` calls
    ``build_function_declaration(func=...)`` to introspect the Python
    signature. We override it to return the matching entry from
    ``TOOL_DECLARATIONS`` so every field — types, enums, required, per-arg
    descriptions — is exactly what we wrote.

    Execution (``run_async``) still delegates to the bound Python callable,
    so the rest of ADK (context vars, logging, result propagation) works
    unchanged.
    """

    def __init__(self, func: Callable, declaration: types.FunctionDeclaration):
        super().__init__(func=func)
        # ADK's BaseTool exposes ``name`` and ``description`` to the runtime.
        # Override them so list_tools() reflects the hand-written metadata
        # rather than the Python __name__ / __doc__ (which may differ).
        self._declaration = declaration
        self.name = declaration.name
        self.description = declaration.description or ""

    def _get_declaration(self) -> Optional[types.FunctionDeclaration]:
        return self._declaration


def _make_tool(func: Callable) -> SchemaFunctionTool:
    """Pair an execution wrapper with its hand-written declaration.

    Requires a declaration for ``func.__name__`` in TOOL_DECLARATIONS —
    KeyError at import time if missing, which is the intended fail-fast.
    """
    return SchemaFunctionTool(func=func, declaration=TOOL_DECLARATIONS[func.__name__])


# ─────────────────────────────────────────────────────────
# ALL TOOLS FOR ADK
# Export this list to pass to LlmAgent
# ─────────────────────────────────────────────────────────

ALL_TOOLS: list[SchemaFunctionTool] = [
    _make_tool(get_user_profile),
    _make_tool(register_user),
    _make_tool(get_order_support),
    _make_tool(set_chat_preferences),
    _make_tool(record_recipient_preferences),
    _make_tool(add_occasion),
    _make_tool(list_occasions),
    _make_tool(get_my_occasions),
    _make_tool(get_occasion_details),
    _make_tool(search_brands),
    _make_tool(suggest_gifts),
    _make_tool(save_gift_plan),
    _make_tool(select_gift_candidate),
    _make_tool(create_checkout),
    _make_tool(suggest_marketplace_gift_card),
    _make_tool(get_self_preferences),
    _make_tool(get_brand_details),
    _make_tool(create_gift_order),
    _make_tool(list_orders),
    _make_tool(check_occasion_gifts),
    _make_tool(log_external_purchase),
    _make_tool(log_my_gift),
    _make_tool(add_to_wishlist),
    _make_tool(list_my_wishlist),
    _make_tool(remove_wishlist_item),
    _make_tool(clear_wishlist),
    _make_tool(get_upcoming_digest),
    _make_tool(snooze_occasion),
    _make_tool(skip_occasion_this_year),
    _make_tool(get_monthly_spend),
    _make_tool(create_batch_gift_orders),
    _make_tool(setup_celebrant_occasion),
    _make_tool(update_celebrant_occasion),
    _make_tool(get_celebrant_context),
    _make_tool(create_friend_gift_order),
    _make_tool(edit_occasion),
    _make_tool(delete_occasion),
    _make_tool(delete_all_occasions),
    _make_tool(merge_occasions),
    _make_tool(add_to_cart),
    _make_tool(view_cart),
    _make_tool(checkout_cart),
    _make_tool(clear_cart),
    _make_tool(search_vendors),
    _make_tool(set_contact_city),
    _make_tool(get_reminder_schedule),
    _make_tool(get_reminder_history),
    _make_tool(set_reminder_schedule),
    _make_tool(send_reminder_now),
    _make_tool(get_friend_wishlist),
    _make_tool(reserve_wishlist_item),
    _make_tool(confirm_wishlist_purchase),
    _make_tool(request_recipient_preferences),
]

# Per-tool lookup for phase-based partitioning in agent.py — maps the tool
# name (identical to the callable's __name__) to its SchemaFunctionTool.
TOOL_BY_NAME: dict[str, SchemaFunctionTool] = {}
for t in ALL_TOOLS:
    if not t.name:
        raise RuntimeError("SchemaFunctionTool.name is missing; check TOOL_DECLARATIONS")
    TOOL_BY_NAME[t.name] = t
