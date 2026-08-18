"""ADK-based WhatsApp agent orchestration.

Single entry point: handle_message(db, phone, text) -> str

This module owns routing, phase-specific tool exposure, ADK sessions,
timeouts, circuit breaking, and response fallback/salvage.

Architecture:
- prompt.py: Static system prompt
- tools/schemas.py: Gemini-facing FunctionDeclarations
- tools/adapter.py: SchemaFunctionTool, TOOL_BY_NAME, execution bridge
- tools/functions.py: Async tool implementations
- action_router.py: Deterministic pre-LLM routes
"""

import asyncio
import contextlib
import hashlib
import itertools
import json
from functools import lru_cache
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, Session
from google.genai import errors as genai_errors, types
from sqlalchemy import func, select, text as sql_text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    CampaignContext,
    CelebrantFriend,
    CelebrantOccasion,
    Contact,
    ConversationMessage,
    LlmUsageEvent,
    MessageRole,
    Occasion,
    User,
)
from app.models.conversation import ConversationState
from app.bot.action_router import (
    try_handle_actions,
    try_handle_exact_phrases,
    try_handle_product_urls_before_classifier,
)
from app.bot.whatsapp_templates import acquisition_greeting_for
from app.bot.prompt import STATIC_SYSTEM_PROMPT
from app.bot.intent_classifier import IntentResult
from app.bot.intent_router import RouteResult, route as route_intent
from app.bot.language_map import active_gift_context_updates_from_text
from app.bot.rate_limit import check_and_bump
from app.bot.tools import set_context, reset_context
from app.bot.tools.adapter import TOOL_BY_NAME
from app.bot.tools.functions import CART_TTL_HOURS
from app.utils.circuit_breaker import CircuitBreaker
from app.utils.dates import next_occurrence
from app.utils.link_utils import extract_urls, is_product_link
from app.utils.observability import latency_bucket, mask_phone, metrics
from app.utils.phone_hash import hash_phone
from app.utils.llm_cost import estimate_cost
from app.utils.budget_guard import check_all_budgets, get_daily_budget_usage
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.agent")
_ALLOW_SENSITIVE_LOGS = settings.app_env != "production"

MVP_HIDDEN_TOOLS = frozenset({
    "select_gift_candidate",
    "create_checkout",
    "search_brands",
    "get_brand_details",
    "suggest_marketplace_gift_card",
    "create_gift_order",
    "create_friend_gift_order",
    "create_batch_gift_orders",
    "add_to_cart",
    "checkout_cart",
    "search_vendors",
    "set_contact_city",
    "list_orders",
    "get_monthly_spend",
    "check_occasion_gifts",
})


def _adk_session_db_url() -> str:
    """Return a SYNC-driver URL for ADK's DatabaseSessionService.

    ADK 1.2.x's ``DatabaseSessionService.__init__`` calls
    ``sqlalchemy.create_engine(db_url)`` — that's the SYNC engine, not
    the async one. It expects a sync DBAPI (psycopg2). Passing a
    ``postgresql+asyncpg://`` URL makes SQLAlchemy try to drive asyncpg
    synchronously, which blows up with ``MissingGreenlet`` the moment
    ADK calls ``inspect(engine)``.

    Meanwhile the app's main engine (``app/db/session.py``) uses
    ``create_async_engine`` and REQUIRES ``+asyncpg`` — which the config
    validator normalises into ``settings.database_url``.

    This helper bridges the two needs: strip the async-driver hint so
    ADK gets the bare ``postgresql://`` URL it needs. Non-Postgres URLs
    (SQLite for tests) pass through unchanged.
    """
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def _preview_text(value: object, limit: int = 200) -> str:
    text = str(value)
    if _ALLOW_SENSITIVE_LOGS:
        return text[:limit]
    return f"<redacted len={len(text)}>"


_USAGE_METADATA_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "cached_content_token_count",
)
_SAFE_GREETING_EXACTS = frozenset({"hi", "hello", "hey", "namaste"})
_NEXT_OCCASION_WINDOW_DAYS = 30
_NEXT_OCCASION_MAX_WINDOW_DAYS = 60


def _usage_metadata_payload(source: object) -> dict[str, int] | None:
    """Extract Gemini token/cache counters from an ADK response/event.

    ADK Event currently inherits LlmResponse, which may expose
    ``usage_metadata`` from the underlying GenerateContentResponse. Keep this
    defensive because older/future ADK layers may not surface usage metadata
    at the runner event layer.
    """
    usage = getattr(source, "usage_metadata", None)
    if usage is None:
        return None

    payload: dict[str, int] = {}
    for field in _USAGE_METADATA_FIELDS:
        value = getattr(usage, field, None)
        if isinstance(value, int):
            payload[field] = value
    return payload or None


def _record_gemini_usage_metrics(phase: str, usage_payload: dict[str, int]) -> None:
    """Accumulate token counters for implicit-cache telemetry."""
    metric_fields = {
        "gemini_prompt_tokens": "prompt_token_count",
        "gemini_cached_tokens": "cached_content_token_count",
        "gemini_total_tokens": "total_token_count",
    }
    for metric_name, field in metric_fields.items():
        value = usage_payload.get(field)
        if isinstance(value, int):
            metrics.inc(metric_name, value)
            metrics.inc(f"{metric_name}.{phase}", value)


# Session service — use ADK's DB-backed implementation so sessions survive
# restarts and stay consistent across multiple workers.
_ADK_SESSION_ENGINE_KWARGS = {
    # Survive DB restarts and recycled private-network sockets.
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
}


def _build_session_service() -> DatabaseSessionService:
    return DatabaseSessionService(
        db_url=_adk_session_db_url(),
        **_ADK_SESSION_ENGINE_KWARGS,
    )


def _dispose_session_service(service: DatabaseSessionService | None) -> None:
    if service is None:
        return
    db_engine = getattr(service, "db_engine", None)
    if db_engine is None:
        return
    with contextlib.suppress(Exception):
        db_engine.dispose()


_session_service = _build_session_service()

# Session limits
MAX_SESSION_EVENTS = 20  # ~10 user/model exchanges — enough to keep a
# multi-turn occasion add in view. At 12 the window dropped facts mid-flow and
# the agent restarted questions it had already asked (2026-08 transcript).
SUMMARY_TRIGGER_EVENTS = 18
STALE_SESSION_SECONDS = 30 * 60  # 30 minutes

# Hard timeout per user turn — prevents a stuck Gemini call from hanging a webhook.
# 12 s covers a realistic 2-tool chain (suggest_gifts → create_gift_order) plus
# Hubble latency. Aggressive enough to bound worst case, generous enough that
# legitimate multi-step turns no longer degrade to the canned fallback. The
# instant ACK still covers perceived latency.
AGENT_TURN_TIMEOUT_SECONDS = 12.0

# Circuit breaker around the Gemini call. Trips after 5 failures (timeouts
# OR exceptions) inside a 30 s window; cools down for 30 s before allowing
# a single probe call. Prevents a Gemini outage from cascading into 5 s+
# wait stacks for every concurrent user.
_gemini_breaker = CircuitBreaker(
    failure_threshold=5, window_seconds=30.0, cooldown_seconds=30.0,
)
_GEMINI_OUTAGE_FALLBACK = (
    "I'm having trouble reaching the AI right now — please try again in a moment 🙏"
)

# Rotates the empty-reply fallback wording so consecutive fallbacks never
# share a body (the outbound dedup drops identical bodies within 60s,
# which previously left the user with silence on the second fallback).
_EMPTY_FALLBACK_COUNTER = itertools.count()


def _is_gemini_error(exc: BaseException) -> bool:
    """Return True only for exceptions attributable to the Gemini call itself.

    `_consume_events` runs the full ADK loop including tool execution, so a
    raised exception could come from Gemini, the network, the DB, Hubble,
    or one of our tools. We only want the breaker to track Gemini health,
    so we whitelist:
      * `google.genai.errors.APIError` and subclasses (ClientError, ServerError,
        UnknownApiResponseError) — explicit SDK errors
      * `httpx.TransportError` / `httpx.HTTPStatusError` — network layer
        below the SDK (Gemini SDK uses httpx internally)
    Everything else (DB IntegrityError, our `ToolError`, asyncio cancels, …)
    is treated as out-of-scope and does NOT trip the breaker.
    """
    import httpx as _httpx

    return isinstance(
        exc,
        (
            genai_errors.APIError,
            _httpx.TransportError,
            _httpx.HTTPStatusError,
        ),
    )

# ─────────────────────────────────────────────────────────
# PHASE-BASED TOOL PARTITIONING
# Exposing 30 tools every turn degrades Gemini tool selection accuracy.
# We gate the visible tool set on conversation phase:
#   * unregistered → onboarding only
#   * friend       → friend-of-celebrant flow
#   * checkout     → active cart → payment
#   * browse       → default registered-user flow
# Each phase gets its own cached LlmAgent + Runner. All runners share the
# same session_service and app_name, so conversation history is continuous
# even when the user crosses phases within a single conversation.
# ─────────────────────────────────────────────────────────

def _tools(*names: str) -> list:
    """Resolve tool names to their SchemaFunctionTool instances.

    Each phase lists tool names (strings) for readability. Lookup hits
    TOOL_BY_NAME, whose SchemaFunctionTool entries override ADK's generated
    declarations with the hand-written declarations from schemas.py.
    """
    return [TOOL_BY_NAME[n] for n in names]


def _mvp_visible_tools(tools: list) -> list:
    """Hide marketplace/payment/provider tools from Gemini in reminder MVP."""
    if not settings.mvp_reminder_only:
        return tools
    visible = [tool for tool in tools if tool.name not in MVP_HIDDEN_TOOLS]
    if not settings.preference_outbound_enabled:
        visible = [tool for tool in visible if tool.name != "request_recipient_preferences"]
    if not settings.enable_shared_wishlist:
        visible = [
            tool for tool in visible
            if tool.name not in {
                "get_friend_wishlist",
                "reserve_wishlist_item",
                "confirm_wishlist_purchase",
            }
        ]
    return visible


_UNREGISTERED_TOOLS = _tools(
    "get_user_profile",
    "register_user",
)

_FRIEND_TOOLS = _tools(
    "get_user_profile",
    "register_user",
    "get_celebrant_context",
    "suggest_gifts",
    "save_gift_plan",
    "select_gift_candidate",
    "create_checkout",
    "search_brands",
    "get_brand_details",
    "suggest_marketplace_gift_card",
    "create_friend_gift_order",
    "log_external_purchase",
    "add_to_wishlist",
    "list_my_wishlist",
    "remove_wishlist_item",
    "clear_wishlist",
    "get_friend_wishlist",
    "reserve_wishlist_item",
    "confirm_wishlist_purchase",
    "get_reminder_schedule",
    "send_reminder_now",
)

_BROWSE_TOOLS = _tools(
    "get_user_profile",
    "get_my_occasions",
    "add_occasion",
    "edit_occasion",
    "delete_occasion",
    "delete_all_occasions",
    "merge_occasions",
    "get_occasion_details",
    "suggest_gifts",
    "select_gift_candidate",
    "create_checkout",
    "search_brands",
    "get_brand_details",
    "suggest_marketplace_gift_card",
    "get_self_preferences",
    "create_gift_order",
    "create_batch_gift_orders",
    "add_to_cart",
    "view_cart",
    "checkout_cart",
    "get_upcoming_digest",
    "snooze_occasion",
    "skip_occasion_this_year",
    "log_external_purchase",
    "log_my_gift",
    "add_to_wishlist",
    "list_my_wishlist",
    "remove_wishlist_item",
    "clear_wishlist",
    "setup_celebrant_occasion",
    "list_orders",
    "get_monthly_spend",
    "check_occasion_gifts",
    "get_reminder_schedule",
    "get_reminder_history",
    "set_reminder_schedule",
    "send_reminder_now",
    "request_recipient_preferences",
)

# Checkout phase = browse's full surface PLUS `clear_cart` (not in browse
# because it's destructive; we only surface it when the user clearly has
# a cart). A narrower checkout phase used to backfire: an abandoned cart
# item trapped users out of `suggest_gifts` and the model would
# hallucinate tool calls that weren't registered — see issue #009 §5b
# Gap G7 (stale cart traps user in narrow phase).
_CHECKOUT_TOOLS = list(_BROWSE_TOOLS) + _tools("clear_cart")

_PHASE_TOOLS: dict[str, list] = {
    "unregistered": _mvp_visible_tools(_UNREGISTERED_TOOLS),
    "friend": _mvp_visible_tools(_FRIEND_TOOLS),
    "checkout": _mvp_visible_tools(_CHECKOUT_TOOLS),
    "browse": _mvp_visible_tools(_BROWSE_TOOLS),
}

# Cache one Runner per phase. All share _session_service so sessions (and
# tool-call history) carry across phase transitions.
_runners: dict[tuple[str, str, tuple[str, ...]], Runner] = {}


def _reset_session_service() -> None:
    """Rebuild the ADK session service after a transient DB disconnect."""
    global _session_service
    old_service = _session_service
    _session_service = _build_session_service()
    _runners.clear()
    _dispose_session_service(old_service)
    logger.warning("Rebuilt ADK session service after transient DB disconnect")


_TRANSIENT_DB_MARKERS = (
    "server closed the connection unexpectedly",
    "terminating connection",
    "could not connect to server",
    "connection refused",
    "connection reset",
    "connection not open",
    "connection is closed",
    "connection already closed",
    "broken pipe",
    "ssl connection has been closed unexpectedly",
)


def _is_transient_db_disconnect(exc: BaseException) -> bool:
    """Return True when a DB exception looks like a connection drop/restart."""
    # Bare Python socket/OS errors are always transient.
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        return True
    # SQLAlchemy-level: OperationalError and InterfaceError both signal lost
    # connections depending on the psycopg2 code path.
    if not isinstance(exc, (OperationalError, InterfaceError)):
        return False
    if getattr(exc, "connection_invalidated", False):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_DB_MARKERS)


async def _session_service_call(method_name: str, **kwargs):
    """Call an ADK session-service method and retry once on DB restart noise.

    Catches OperationalError, InterfaceError (both SQLAlchemy-wrapped psycopg2
    variants), and bare OS-level connection errors.  Non-transient errors
    (ProgrammingError, IntegrityError, DataError, …) are never retried.
    """
    for attempt in (1, 2):
        method = getattr(_session_service, method_name)
        try:
            return await method(**kwargs)
        except (OperationalError, InterfaceError, ConnectionRefusedError, ConnectionResetError) as exc:
            if attempt == 1 and _is_transient_db_disconnect(exc):
                logger.warning(
                    "ADK session service %s hit a transient DB disconnect (%s); rebuilding and retrying once",
                    method_name,
                    type(exc).__name__,
                )
                _reset_session_service()
                continue
            logger.error(
                "ADK session service %s failed on attempt %d: %s",
                method_name,
                attempt,
                exc,
                exc_info=True,
            )
            raise


def _end_invocation_on_user_message(
    tool: Any, args: dict[str, Any], tool_context: Any, tool_response: dict
) -> None:
    """Stop the turn when a failed tool already wrote the user-facing reply.

    ADK's own cooperative stop. Breaking out of `runner.run_async` instead throws
    GeneratorExit into the generator mid-iteration, which unwinds OpenTelemetry's
    `invocation` span from a different async context and logs "Failed to detach
    context" (harmless, but real Sentry noise). Setting end_invocation lets the
    generator finish on its own, so no second LLM call is made and no span is
    torn down out of order.
    """
    if not isinstance(tool_response, dict) or not tool_response.get("error"):
        return None
    direct = tool_response.get("user_message")
    if isinstance(direct, str) and direct.strip():
        tool_context.invocation_context.end_invocation = True
    return None


def _build_runner(phase: str, model: str, tools: list) -> Runner:
    # gemini-2.5-flash thinking can consume 800-1000 tokens before any
    # visible output. With max=1024, replies got truncated mid-sentence
    # (finish_reason=MAX_TOKENS, candidates_token_count=39 observed).
    # 2048 leaves ~1k headroom for the visible reply after thinking.
    gen_config = types.GenerateContentConfig(
        temperature=settings.gemini_temperature,
        top_p=0.9,
        max_output_tokens=2048,
    )
    # Gemini sees declarations through tools=tools. These tools come from
    # TOOL_BY_NAME, which wraps schemas.py FunctionDeclarations via
    # SchemaFunctionTool while preserving the adapter execution wrappers.
    # Bound the model's internal reasoning. Unbounded, 2.5-flash spent 1399
    # thinking tokens on a 27-token reply (7.6s server-side, 59% of turn cost —
    # thinking bills at the output rate). Thinking, not prompt size, is the
    # dominant latency term on this workload. ADK rejects thinking_config inside
    # generate_content_config and requires a planner instead.
    agent = LlmAgent(
        name=f"wishkr_{phase}",
        model=model,
        instruction=STATIC_SYSTEM_PROMPT,
        tools=tools,
        generate_content_config=gen_config,
        after_tool_callback=_end_invocation_on_user_message,
        planner=BuiltInPlanner(
            thinking_config=types.ThinkingConfig(
                thinking_budget=settings.gemini_thinking_budget,
            ),
        ),
    )
    runner = Runner(
        agent=agent,
        app_name="wishkr",
        session_service=_session_service,
    )
    logger.info(
        "Cached LlmAgent[%s] (model=%s, tools=%d, temp=%.2f)",
        phase, model, len(tools), settings.gemini_temperature,
    )
    return runner


def _get_runner_for_phase(
    phase: str,
    model: str | None = None,
    tools: list | None = None,
) -> Runner:
    model = model or settings.gemini_model
    tools = tools or _PHASE_TOOLS[phase]
    key = (phase, model, tuple(t.name for t in tools))
    runner = _runners.get(key)
    if runner is None:
        runner = _build_runner(phase, model, tools)
        _runners[key] = runner
    return runner


def _pick_phase(
    user: User | None,
    celebrant_occasion_id: str | None,
    cart_count: int,
) -> str:
    """Select the tool-partition phase for this turn.

    Priority (highest first):
      1. celebrant_occasion_id present → 'friend' flow dominates even if the
         user happens to have a cart from a separate thread.
      2. user not registered → 'unregistered' onboarding.
      3. cart has items → 'checkout' so clear_cart is visible.
      4. default → 'browse'.
    """
    if celebrant_occasion_id:
        return "friend"
    if user is None:
        return "unregistered"
    if cart_count > 0:
        return "checkout"
    return "browse"


_WISHLIST_SUBFLOW_TOOLS = {
    "get_user_profile",
    "add_to_wishlist",
    "list_my_wishlist",
    "remove_wishlist_item",
    "clear_wishlist",
}
_PAYMENT_SUBFLOW_TOOLS = {
    "get_user_profile",
    "list_orders",
    "create_gift_order",
    "create_checkout",
    "create_batch_gift_orders",
    "add_to_cart",
    "view_cart",
    "checkout_cart",
    "clear_cart",
    "check_occasion_gifts",
}
_PREF_SUBFLOW_TOOLS = {
    "get_user_profile",
    "get_self_preferences",
    "request_recipient_preferences",
    "get_my_occasions",
    "get_occasion_details",
}
_DISCOVERY_SUBFLOW_TOOLS = {
    "get_user_profile",
    "get_my_occasions",
    "suggest_gifts",
    "save_gift_plan",
    "select_gift_candidate",
    "create_checkout",
    "search_brands",
    "get_brand_details",
    "suggest_marketplace_gift_card",
    "get_self_preferences",
    # Gift asks routinely name a date ("birthday gift for mom"), and reminders
    # are the core product — without this the agent cannot save that occasion in
    # the same turn and the user has to repeat themselves.
    "add_occasion",
}
_OCCASION_SUBFLOW_TOOLS = {
    "get_user_profile",
    "get_my_occasions",
    "add_occasion",
    "edit_occasion",
    "delete_occasion",
    "update_celebrant_occasion",
    "get_occasion_details",
    "get_upcoming_digest",
    "snooze_occasion",
    "skip_occasion_this_year",
    "get_reminder_schedule",
    "get_reminder_history",
    "set_reminder_schedule",
    "send_reminder_now",
    # Occasion talk and gift talk co-occur constantly ("it's Riya's
    # birthday, what should I get her?") — keep suggestion/logging in
    # scope so the narrowed turn can't strand a gift ask.
    "suggest_gifts",
    "save_gift_plan",
    "log_my_gift",
}
_SELECTIVE_EXTENSION_TOOLS = {
    # Keep specialized write tools out of the already-large default schema,
    # but expose them on the turns where their intent is detected.
    "save_gift_plan",
    "update_celebrant_occasion",
}


def _is_critical_turn(text: str, phase: str, cart_count: int) -> bool:
    """Critical turns are never downgraded/blocked by budget policy."""
    if phase == "checkout" or cart_count > 0:
        return True
    lower = (text or "").lower()
    critical_markers = ("payment", "pay ", "checkout", "razorpay", "webhook", "order", "refund")
    return any(m in lower for m in critical_markers)


@lru_cache(maxsize=256)
def _routed_tool_names(named: tuple[str, ...]) -> frozenset[str]:
    """Expand router-named tools with the ones a turn usually needs alongside.

    Follows `depends_on` FORWARD only — what a named tool needs to do its job
    (edit_occasion → get_my_occasions, to resolve the id first).

    Deliberately not reverse-expanded. Pulling in every tool that depends on a
    named one sounds helpful but inverts the goal: `get_my_occasions` is a
    dependency of most occasion tools, so naming it dragged in 14 of 24
    declarations and narrowed almost nothing. Forward-only keeps an occasion
    save at 2 tools.

    Registry-driven on purpose: a newly registered tool joins the right cluster
    from its own metadata, with nothing to edit here.
    """
    from app.bot.tools.schemas import TOOL_SPECS

    wanted: set[str] = set()
    frontier = list(named)
    while frontier:
        name = frontier.pop()
        if name in wanted:
            continue
        wanted.add(name)
        spec = TOOL_SPECS.get(name)
        if spec:
            # Transitive: a dependency may have its own prerequisites.
            frontier.extend(spec.depends_on)
    return frozenset(wanted)


def _ensure_friend_context_tool(phase: str, selected: list) -> list:
    """Keep the friend-context source of truth available on narrowed turns.

    A friend-phase prompt explicitly requires ``get_celebrant_context`` before
    generic gifting. Tool narrowing previously removed that tool from gift
    discovery turns, leaving the model unable to follow its instruction.
    """
    if phase != "friend" or any(
        tool.name == "get_celebrant_context" for tool in selected
    ):
        return selected
    return [*selected, TOOL_BY_NAME["get_celebrant_context"]]


def _select_tools_for_turn(
    phase: str,
    text: str,
    intent_result: IntentResult | RouteResult,
) -> tuple[list, str]:
    """Conservative per-turn tool narrowing for lower prompt/tool token load."""
    tools = _PHASE_TOOLS[phase]
    if phase == "unregistered":
        return tools, "unregistered"

    # Preferred path: the intent router named the tools it expects. Driven by
    # the tool registry (unknown names are already dropped in intent_router),
    # so registering a new tool makes it routable with no edit here.
    #
    # Related tools are pulled in alongside the named ones via `depends_on`:
    # narrowing to exactly one tool would strand a turn that needs a lookup
    # first (e.g. edit_occasion without get_my_occasions to find the id).
    named = tuple(getattr(intent_result, "likely_tools", ()) or ())
    if named:
        wanted = _routed_tool_names(named)
        selected = [tool for tool in tools if tool.name in wanted]
        # A single-tool turn is legitimate and common — an occasion save is
        # exactly one add_occasion. Only widen when the router named nothing
        # this phase actually exposes (e.g. a checkout tool while purchases
        # are disabled), where a narrowed set would strand the turn.
        if selected:
            return _ensure_friend_context_tool(phase, selected), "routed"
        logger.info(
            "routed tools %s are not exposed in phase — using full phase set",
            list(named),
        )
        return tools, "routed_wide"

    # Degraded path: the router was unavailable, so fall back to keyword
    # signals. Intentionally kept for outages only — not the primary route.
    lower = (text or "").lower()
    wishlist_signal = "wishlist" in lower
    payment_signal = any(k in lower for k in ("payment", "pay ", "order", "checkout", "cart"))
    pref_signal = any(k in lower for k in ("preference", "preferences", "recipient"))
    occasion_signal = any(
        k in lower
        for k in ("occasion", "ocassion", "reminder", "remind", "birthday",
                  "anniversary", "snooze", "upcoming")
    )
    # The classifier is the primary signal here: "gift for my sister under 2000"
    # names no product, so it lands on gift_suggestion rather than
    # product_question. Before that label existed it fell through to `default`
    # and shipped all 24 declarations (~18.3k tokens) for the single most common
    # request shape. Keywords stay as a backstop for when the classifier is
    # unavailable and everything degrades to "other".
    discovery_signal = intent_result.intent in ("product_question", "gift_suggestion") or any(
        k in lower for k in ("suggest", "gift idea", "gift ideas", "brands", "vouchers")
    )

    subset: set[str] | None = None
    subflow = "default"
    if wishlist_signal:
        subset = _WISHLIST_SUBFLOW_TOOLS
        subflow = "wishlist"
    elif payment_signal:
        subset = _PAYMENT_SUBFLOW_TOOLS
        subflow = "payment"
    elif pref_signal:
        subset = _PREF_SUBFLOW_TOOLS
        subflow = "recipient_preference"
    elif discovery_signal:
        subset = _DISCOVERY_SUBFLOW_TOOLS
        subflow = "product_discovery"
    elif occasion_signal:
        subset = _OCCASION_SUBFLOW_TOOLS
        subflow = "occasions"

    if subset is None:
        return tools, subflow

    selected = [tool for tool in tools if tool.name in subset]
    selected_names = {tool.name for tool in selected}
    for name in sorted(subset & _SELECTIVE_EXTENSION_TOOLS):
        if name not in selected_names and name in TOOL_BY_NAME:
            selected.append(TOOL_BY_NAME[name])
    if len(selected) < 3:
        return tools, "default"
    return _ensure_friend_context_tool(phase, selected), subflow


# ─────────────────────────────────────────────────────────
# PER-PHONE SERIALIZATION
# Two near-simultaneous messages from the same phone would otherwise race
# on the shared ADK session's event log. A lightweight asyncio.Lock keyed by
# phone serializes them without needing a session-store swap.
# ─────────────────────────────────────────────────────────

_phone_locks: dict[str, asyncio.Lock] = {}
_phone_locks_guard = asyncio.Lock()


async def _acquire_phone_lock(phone: str) -> asyncio.Lock:
    """Get-or-create the per-phone lock. The guard itself serializes the
    get-or-create step so two coroutines don't race on dict insertion."""
    async with _phone_locks_guard:
        lock = _phone_locks.get(phone)
        if lock is None:
            lock = asyncio.Lock()
            _phone_locks[phone] = lock
    return lock


def _gc_phone_locks(max_entries: int = 10_000) -> None:
    """Drop uncontended locks once the dict grows past a ceiling — keeps
    memory bounded when ever-seen phones diverge from active phones."""
    if len(_phone_locks) < max_entries:
        return
    stale = [p for p, l in _phone_locks.items() if not l.locked()]
    for p in stale[: len(_phone_locks) - max_entries // 2]:
        _phone_locks.pop(p, None)


def _active_gift_context_updates_from_text(text: str) -> dict[str, object]:
    """Cheap progressive context capture for short follow-up turns.

    This intentionally handles only high-signal phrases. The LLM still owns
    interpretation; this just prevents obvious context like "BGMI" or
    "headphones" from disappearing between turns.
    """
    return active_gift_context_updates_from_text(text)


# ─────────────────────────────────────────────────────────
# CROSS-WORKER SERIALIZATION
# The in-process asyncio.Lock above only serialises same-phone turns inside
# ONE uvicorn worker. A phone that lands on two workers concurrently (e.g.
# after a Meta webhook retry that hits a different replica) would still race
# on the shared ADK session. A PostgreSQL session-level advisory lock keyed
# by phone closes that gap without requiring new infra — we're already on
# Postgres and the lock is held only for the turn duration.
# ─────────────────────────────────────────────────────────


def _advisory_lock_key(phone: str) -> int:
    """Stable 63-bit int derived from the phone — fits pg_advisory_lock(bigint).

    SHA-256 collisions at 63 bits are negligible for our phone cardinality.
    Returning a signed int in the int64 range that Postgres accepts.
    """
    digest = hashlib.sha256(phone.encode()).digest()
    # Take 8 bytes, clear the sign bit so it fits Postgres bigint positive.
    val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    return val


@contextlib.asynccontextmanager
async def _db_phone_lock(db: AsyncSession, phone: str, *, timeout_s: float = 8.0):
    """Acquire a Postgres transaction advisory lock for this phone.

    Uses ``pg_try_advisory_xact_lock`` in a short poll loop rather than
    cancelling a blocking ``pg_advisory_lock`` query. Cancelling asyncpg while
    SQLAlchemy has an open transaction can invalidate the connection and leave
    the session requiring rollback, which then poisons fallback persistence.
    On SQLite / non-Postgres (dev/test) the locks gracefully no-op.

    The lock is held until the current transaction commits or rolls back. If it
    cannot be acquired before ``timeout_s``, we proceed without the cross-worker
    lock but keep the session healthy.
    """
    key = _advisory_lock_key(phone)
    dialect = db.bind.dialect.name if db.bind is not None else ""
    is_pg = dialect == "postgresql"

    if is_pg:
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                result = await db.execute(
                    sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": key},
                )
                if result.scalar_one():
                    break
                if time.monotonic() >= deadline:
                    logger.warning(
                        "[%s] advisory lock timeout after %.1fs — proceeding without cross-worker lock",
                        phone, timeout_s,
                    )
                    break
                await asyncio.sleep(0.05)
        except Exception as e:
            # Any DB error on lock acquire — don't block the turn, just log.
            logger.warning("[%s] advisory lock acquire failed: %s", phone, e)
            try:
                await db.rollback()
            except Exception:
                logger.exception("[%s] rollback after advisory lock failure failed", phone)

    yield


async def _build_dynamic_tail(
    db: AsyncSession,
    phone: str,
    user: User | None,
    celebrant_occasion_id: str | None,
) -> str:
    """Build only the DYNAMIC tail (appended after STATIC_SYSTEM_PROMPT).

    Lean by design: only the minimum context the agent can't get from a tool
    call. No counts, no instructional notes, no menu — those live in tools or
    the static prompt.
    """
    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    parts = [f"Today: {today}", f"Phone: {phone}"]

    if user and user.name:
        parts.append(f"User: {user.name} ({user.city_raw or 'Unknown'})")
        parts.append("Registered: yes")

        result = await db.execute(
            select(Occasion)
            .where(Occasion.user_id == user.id, Occasion.deleted_at.is_(None))
            .order_by(Occasion.date)
            .limit(3)
        )
        occasions = result.scalars().all()

        if occasions:
            now = datetime.now(timezone.utc)
            upcoming = sorted(
                [(o, next_occurrence(o, now)) for o in occasions],
                key=lambda pair: pair[1],
            )
            # Only surface "Next" occasion context when it's genuinely near-term.
            # This prevents stale-feeling lines like "upcoming ... on 08 May"
            # when that date is effectively next year.
            in_30_days = [
                (o, dt)
                for o, dt in upcoming
                if 0 <= (dt.date() - now.date()).days <= _NEXT_OCCASION_WINDOW_DAYS
            ]
            in_60_days = [
                (o, dt)
                for o, dt in upcoming
                if 0 <= (dt.date() - now.date()).days <= _NEXT_OCCASION_MAX_WINDOW_DAYS
            ]
            next_pair = in_30_days[0] if in_30_days else (in_60_days[0] if in_60_days else None)

            if next_pair:
                occ, date = next_pair
                label = occ.custom_label or occ.type.value.replace("_", " ").title()
                # Resolve the recipient's display name so the agent doesn't
                # ask "who is this gift for?" when the user has already told
                # us their friend's name on an earlier turn (e.g. "Swaraj's
                # birthday today"). Without this the trim window may drop
                # the introduction and the agent loses recipient context.
                recipient_name: str | None = None
                if occ.contact_id:
                    contact = await db.get(Contact, occ.contact_id)
                    if contact and contact.name:
                        recipient_name = contact.name.strip() or None
                # Surface the actual occasion_id so the model can pass it to
                # tools (send_reminder_now, edit_occasion, …) without having
                # to call get_my_occasions or — worse — fabricate one from the
                # friend-context cel_* id.
                if recipient_name:
                    parts.append(
                        f"Next: {recipient_name}'s {label} on {date.strftime('%d %b %Y')} (occasion_id={occ.id})"
                    )
                    # "Active flow" means the occasion is happening within
                    # +/- 14 days of today (so the user is plausibly mid-
                    # gift-decision for this friend). We compare against
                    # the closest occurrence — either the stored date or
                    # this-year's anniversary, whichever is nearer to now.
                    candidate_dates = [occ.date]
                    if occ.date is not None:
                        try:
                            candidate_dates.append(occ.date.replace(year=now.year))
                        except ValueError:
                            pass
                    nearest_days = min(
                        abs((d.date() - now.date()).days)
                        for d in candidate_dates
                        if d is not None
                    )
                    if nearest_days <= 14:
                        parts.append(
                            f"ActiveRecipient: {recipient_name} (gift-for-friend flow). "
                            "Assume product/brand questions are for this recipient unless "
                            "the user says otherwise. Do NOT ask 'who is this gift for?' "
                            "again and do NOT route to the caller's own wishlist."
                        )
                else:
                    parts.append(
                        f"Next: {label} on {date.strftime('%d %b %Y')} (occasion_id={occ.id})"
                    )
    else:
        parts.append("Registered: no")

    if celebrant_occasion_id:
        parts.append(f"FriendContext: {celebrant_occasion_id}")
        # Without this directive, after a stale-session reset the model has
        # the ID but no celebrant data and asks the user generic onboarding
        # questions ("what do they like?") instead of using the reminder
        # context. Force the tool call when the celebrant data isn't already
        # in this turn's history.
        parts.append(
            "FRIEND FLOW: you're responding to a gift reminder. If the "
            "celebrant's name/interests aren't already in this turn's "
            "history, call get_celebrant_context FIRST — do not ask the "
            "user what the celebrant likes."
        )

    state_result = await db.execute(
        select(ConversationState).where(ConversationState.phone == phone)
    )
    state = state_result.scalar_one_or_none()
    if state and state.temp_data:
        try:
            temp = json.loads(state.temp_data)
            if isinstance(temp, dict):
                from app.bot.state_utils import (
                    OCCASION_SLOTS_KEY,
                    active_gift_context_is_recent,
                    get_active_gift_context,
                )
                # Half-finished occasion add: facts the user gave over earlier
                # turns ("Brother" before the name arrived). Surfacing them
                # keeps the relation from being lost across the clarifying
                # question — the trim window may have dropped the original.
                slots = temp.get(OCCASION_SLOTS_KEY)
                if isinstance(slots, dict) and slots:
                    known = ", ".join(f"{k}={v}" for k, v in sorted(slots.items()))
                    parts.append(
                        f"Occasion in progress (already told to you): {known}. "
                        "Treat the user's next reply as the missing piece of THIS "
                        "occasion — do not start over and do not list saved "
                        "occasions. Pass these values to add_occasion."
                    )
                gift_ctx = get_active_gift_context(temp)
                if active_gift_context_is_recent(gift_ctx):
                    recipient = gift_ctx.get("recipient_name") or "the recipient"
                    occasion = gift_ctx.get("occasion_type") or "occasion"
                    date_text = gift_ctx.get("occasion_date")
                    parts.append("Active gift context:")
                    when = f" on {date_text}" if date_text else ""
                    if gift_ctx.get("purpose") == "gift_for_friend":
                        parts.append(
                            f"- You are helping the user choose a gift for {recipient}'s {occasion}{when}."
                        )
                    else:
                        parts.append("- You are helping the user choose a gift for themselves.")
                    interest = gift_ctx.get("interest") or gift_ctx.get("recipient_vibe")
                    if interest:
                        parts.append(f"- Known interest: {interest}.")
                    if gift_ctx.get("gift_category"):
                        parts.append(f"- Current gift direction: {gift_ctx['gift_category']}.")
                    if gift_ctx.get("selected_brand"):
                        parts.append(f"- Selected brand: {gift_ctx['selected_brand']}.")
                    budget_min = gift_ctx.get("budget_min")
                    budget_max = gift_ctx.get("budget_max")
                    if budget_min is not None and budget_max is not None:
                        parts.append(f"- Budget: ₹{budget_min}-₹{budget_max}.")
                    elif budget_max is not None:
                        parts.append(f"- Budget: up to ₹{budget_max}.")
                    parts.append(
                        "- Do not ask who the gift is for unless the user changes recipient."
                    )
                summary = (temp.get("session_summary") or "").strip()
                if summary:
                    parts.append(f"RecentContextSummary: {summary[:700]}")
        except (json.JSONDecodeError, TypeError):
            pass

    # Surface persisted budget so the agent doesn't lose it after the
    # turn it was first mentioned. Set/cleared in state_utils.
    from app.bot.state_utils import get_budget
    budget = await get_budget(db, phone)
    if budget:
        if budget.get("unlimited"):
            parts.append("Budget: unlimited")
        else:
            mn, mx = budget.get("min"), budget.get("max")
            if mn is not None and mx is not None and mn != mx:
                parts.append(f"Budget: ₹{mn}–₹{mx}")
            elif mx is not None:
                parts.append(f"Budget: ~₹{mx}")
            elif mn is not None:
                parts.append(f"Budget: from ₹{mn}")

    return "\n".join(parts)


def _last_model_question(session: Session) -> str | None:
    """Return the last assistant message text from the session, if any.

    Used to render grounded affirmative hints instead of asking the model to
    "continue the last question" blindly (which hallucinates when the trim
    window dropped that question).
    """
    for event in reversed(session.events):
        content = getattr(event, "content", None)
        if not content or getattr(content, "role", None) != "model":
            continue
        parts = getattr(content, "parts", None) or []
        texts = [p.text for p in parts if getattr(p, "text", None)]
        if texts:
            return " ".join(texts).strip()
    return None


def _intent_hint(result: IntentResult, session: Session) -> str:
    """Render a short intent hint appended to the user message preface.

    For AFFIRMATIVE we only emit a hint if we can quote the actual last
    question from session history — otherwise we stay silent rather than
    prompting the model to invent a referent.
    """
    if result.intent == "affirmative":
        last_q = _last_model_question(session)
        if not last_q:
            # No prior question in session (stale-session reset / trim window)
            # but the user said "yes" to SOMETHING. Greeting them back is
            # jarring — they thought they were mid-flow. Tell the model to
            # ask one short clarifier instead of resetting.
            return (
                "\nUser intent: AFFIRMATIVE but the prior question is no longer "
                "in this session's history (likely a session refresh). Ask ONE "
                "short clarifier — e.g. 'happy to help — what would you like to "
                "do?' — DO NOT greet them as if it's a new conversation."
            )
        snippet = last_q[:240]
        if "?" not in last_q:
            # Your last message was a STATEMENT, so "ok" is an acknowledgement,
            # not an answer. "Proceed on that" makes the model reply with an
            # empty pleasantry and drop the task. Push it to deliver instead.
            return (
                "\nUser intent: ACKNOWLEDGEMENT of your previous message:\n"
                f"> {snippet}\n"
                "They are waiting on YOU. If that message promised something, "
                "deliver it now with a tool call. If you cannot, say so plainly "
                "and give one concrete next step. Do NOT reply with a bare "
                "'happy to help — what next?'."
            )
        return (
            "\nUser intent: AFFIRMATIVE to your previous message:\n"
            f"> {snippet}\n"
            "Proceed on that. If it's unclear in context, ask ONE short clarifier."
        )
    if result.intent == "greeting":
        # If there's a recent assistant question, the "greeting" is more
        # likely confusion than a fresh hello — keep context instead of
        # cold-starting.
        last_q = _last_model_question(session)
        if last_q:
            snippet = last_q[:240]
            return (
                "\nUser intent: possibly CONFUSED (their input looked like a "
                "greeting but you were mid-conversation). Your last message was:\n"
                f"> {snippet}\n"
                "Briefly rephrase or restate that — DO NOT cold-start with "
                "'Hi, I'm Wishkr'."
            )
        return (
            "\nUser intent: GREETING. Greet warmly (by name if registered) and "
            "offer next steps."
        )
    return ""


def _route_hint(intent_result: IntentResult | RouteResult) -> str:
    """Surface the intent router's reading of this turn to Gemini.

    The router already parsed the message with history in view, so passing its
    slots through saves Gemini re-deriving them — and, more importantly, keeps
    multi-occasion messages intact ("Myra 7 Aug / Uttu 9 Aug" arrives as two
    structured entries rather than one that gets half-read).

    Advisory only: Gemini still owns the tool calls and can disagree. The
    router sees a redacted, truncated history, so it can be wrong.
    """
    tools = tuple(getattr(intent_result, "likely_tools", ()) or ())
    slots = getattr(intent_result, "slots", None) or {}
    if not tools and not slots:
        return ""

    parts = ["\nIntent router (advisory — verify against the conversation):"]
    if tools:
        parts.append(f"- Likely tools: {', '.join(tools)}")
    if slots:
        try:
            rendered = json.dumps(slots, ensure_ascii=False)[:600]
        except (TypeError, ValueError):
            rendered = str(slots)[:600]
        parts.append(f"- Parsed details: {rendered}")
        parts.append(
            "  Use these values if they match what the user actually said. "
            "If several occasions are listed, save EVERY one — call the tool "
            "once per occasion. Ask only for details that are genuinely missing."
        )
    return "\n".join(parts)


async def _load_user_context(
    db: AsyncSession, phone: str,
) -> tuple[User | None, str | None, int]:
    """Load user row, celebrant_occasion_id, and cart item count.

    Cart state lives in ConversationState.temp_data["cart_items"] (see
    functions.py::CART_KEY). We lift it here so `_pick_phase` can see it
    without a second DB round-trip.
    """
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()

    celebrant_occasion_id: str | None = None
    cart_count = 0

    state_result = await db.execute(
        select(ConversationState).where(ConversationState.phone == phone)
    )
    state = state_result.scalar_one_or_none()
    if state and state.temp_data:
        try:
            temp = json.loads(state.temp_data)
            if isinstance(temp, dict):
                raw_celebrant_context = temp.get("celebrant_occasion_id")
                if (
                    isinstance(raw_celebrant_context, str)
                    and raw_celebrant_context.strip()
                ):
                    candidate_id = raw_celebrant_context.strip()
                    # Persistent reminder context must not outlive its source
                    # row or the caller's invitation. Otherwise every future
                    # message is forced into the friend phase and a normal
                    # gift-planning reply can surface "reminder no longer
                    # active" months after the reminder was removed.
                    valid_context_id = await db.scalar(
                        select(CelebrantOccasion.id)
                        .join(Occasion, Occasion.id == CelebrantOccasion.occasion_id)
                        .join(
                            CelebrantFriend,
                            CelebrantFriend.celebrant_occasion_id
                            == CelebrantOccasion.id,
                        )
                        .where(
                            CelebrantOccasion.id == candidate_id,
                            CelebrantFriend.phone == phone,
                        )
                    )
                    if valid_context_id:
                        celebrant_occasion_id = valid_context_id

                if (
                    "celebrant_occasion_id" in temp
                    and celebrant_occasion_id is None
                ):
                    temp.pop("celebrant_occasion_id", None)
                    state.temp_data = json.dumps(temp) if temp else None
                    db.add(state)
                    await db.flush()
                    logger.info(
                        "[%s] cleared stale or unauthorized friend context",
                        mask_phone(phone),
                        extra={"type": "stale_friend_context_cleared"},
                    )
                cart = temp.get("cart_items") or []
                if isinstance(cart, list):
                    created_raw = temp.get("cart_created_at")
                    cart_is_fresh = False
                    if isinstance(created_raw, str):
                        try:
                            created_at = datetime.fromisoformat(created_raw)
                            if created_at.tzinfo is None:
                                created_at = created_at.replace(tzinfo=timezone.utc)
                            cart_is_fresh = created_at >= datetime.now(timezone.utc) - timedelta(hours=CART_TTL_HOURS)
                        except ValueError:
                            cart_is_fresh = False
                    cart_count = len(cart) if cart_is_fresh else 0
        except (json.JSONDecodeError, TypeError):
            pass

    return user, celebrant_occasion_id, cart_count


# How long an unconsumed CampaignContext row stays eligible for
# message-match. Past this, a "hi" is treated as organic rather than a
# reply to a stale campaign send.
_CAMPAIGN_CONTEXT_MAX_AGE = timedelta(days=7)


async def _load_and_consume_campaign_context(db: AsyncSession, phone: str) -> str | None:
    """Return the template_name for a fresh, unconsumed campaign send, and mark it consumed.

    Marking-as-consumed happens even if the caller ends up not using the
    result for message-match (e.g. the greeting short-circuit doesn't
    fire this turn) — a campaign context is only meant to color the
    *first* reply, not linger and get replayed on some later turn.
    """
    result = await db.execute(
        select(CampaignContext).where(CampaignContext.phone == phone)
    )
    ctx = result.scalar_one_or_none()
    if ctx is None or ctx.consumed_at is not None:
        return None

    sent_at = ctx.sent_at if ctx.sent_at.tzinfo else ctx.sent_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - sent_at > _CAMPAIGN_CONTEXT_MAX_AGE:
        return None

    ctx.consumed_at = datetime.now(timezone.utc)
    db.add(ctx)
    return ctx.template_name


async def _get_or_create_session(phone: str) -> tuple[Session, bool]:
    """Get existing session or create new one for this phone.

    Returns:
        (session, is_new) — is_new=True if session was just created or refreshed

    Handles:
    - Stale sessions (idle > 30 min) — creates fresh session
    - Session doesn't exist — creates new one
    """
    app_name = "wishkr"

    # Try to get existing session
    sessions = await _session_service_call("list_sessions", app_name=app_name, user_id=phone)
    if sessions and sessions.sessions:
        session = sessions.sessions[0]

        # Check if session is stale
        now = time.time()
        if (now - session.last_update_time) > STALE_SESSION_SECONDS:
            logger.info("Session stale for %s, creating fresh session", phone)
            # Delete old session and create new
            await _session_service_call(
                "delete_session",
                app_name=app_name, user_id=phone, session_id=session.id
            )
            return await _session_service_call(
                "create_session", app_name=app_name, user_id=phone
            ), True

        return session, False

    # Create new session
    return await _session_service_call("create_session", app_name=app_name, user_id=phone), True


_POISONED_ERROR_CODES = frozenset({
    # Server-side crash wrappers from execute_tool. These are almost always
    # caused by a bug that a later deploy fixes — but without scrubbing,
    # Gemini keeps seeing old crash responses in session history on every
    # subsequent turn and concludes "this tool is broken, don't call it",
    # so the fix never takes effect until the session expires.
    "tool_exception",
    # Transient infra failures — same logic, retries should not be gated by
    # yesterday's timeout / connection drop.
    "reminder_dispatch_failed",
})


def _scrub_poisoned_events(session) -> int:
    """Remove function_response events that carry server-crash errors from
    a session IN PLACE before a new turn builds its prompt.

    We deliberately do NOT scrub legitimate structured errors like
    ``occasion_not_found`` or ``user_not_registered`` — those carry
    recovery ``hint``s and the model is supposed to act on them (e.g.
    "call get_my_occasions first"). The target here is narrow: crash
    wrappers where a later deploy has made the tool work again, but the
    session history still insists the tool is broken.

    When a poisoned function_response is dropped we also drop its
    preceding function_call event so pairing stays valid — an orphan
    function_call is the R6 hallucination mode we already fixed for trims.

    Returns the number of events removed (for logging / observability).
    """
    events = getattr(session, "events", None)
    if not events:
        return 0

    poisoned_indices: set[int] = set()
    for idx, event in enumerate(events):
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for part in parts:
            fr = getattr(part, "function_response", None)
            if fr is None:
                continue
            resp = getattr(fr, "response", None) or {}
            if not isinstance(resp, dict):
                continue
            err = resp.get("error")
            if err in _POISONED_ERROR_CODES:
                poisoned_indices.add(idx)
                # Also flag the immediately preceding function_call so the
                # kept history never contains an orphan call with no
                # response — same reason _trim_session_events walks to
                # user-turn boundaries.
                if idx > 0:
                    prev = events[idx - 1]
                    prev_parts = getattr(getattr(prev, "content", None), "parts", None) or []
                    if any(getattr(p, "function_call", None) is not None for p in prev_parts):
                        poisoned_indices.add(idx - 1)
                break

    if not poisoned_indices:
        return 0

    session.events[:] = [e for i, e in enumerate(events) if i not in poisoned_indices]
    return len(poisoned_indices)


async def _trim_session_events(phone: str, session_id: str) -> None:
    """Trim session events to prevent unbounded token growth.

    Keeps roughly the last MAX_SESSION_EVENTS events, but advances the start
    index forward to the next user-turn boundary. This guarantees the kept
    window never begins with an orphan function_response — Gemini hallucinates
    what the matching function_call must have been when it sees one.
    """
    app_name = "wishkr"

    try:
        session = await _session_service_call(
            "get_session",
            app_name=app_name, user_id=phone, session_id=session_id
        )

        if not session or len(session.events) <= MAX_SESSION_EVENTS:
            return

        before = len(session.events)
        start = before - MAX_SESSION_EVENTS
        # Walk forward to a user-authored event so the window starts on a
        # clean turn boundary (never mid function_call/response pair).
        while start < before and getattr(session.events[start], "author", None) != "user":
            start += 1
        # Safety: if nothing qualified, keep only the most recent user event
        # onward rather than corrupting history.
        if start >= before:
            start = before - 1
            while start > 0 and getattr(session.events[start], "author", None) != "user":
                start -= 1
        session.events[:] = session.events[start:]
        logger.info(
            "Trimmed session for %s: %d -> %d events (user-boundary)",
            phone, before, len(session.events),
        )
    except Exception as e:
        logger.warning("Could not trim session for %s: %s", phone, e)


def _safe_compact_session_summary(session: Session) -> str | None:
    """Compact older session turns without touching payment/order semantics."""
    events = getattr(session, "events", None) or []
    if len(events) < SUMMARY_TRIGGER_EVENTS:
        return None

    lines: list[str] = []
    payment_keywords = ("payment", "checkout", "order", "razorpay", "refund")
    for event in events[:-MAX_SESSION_EVENTS]:
        content = getattr(event, "content", None)
        role = getattr(content, "role", None) if content else None
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        text_parts = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
        if not text_parts:
            continue
        text = " ".join(text_parts).strip()
        if not text:
            continue
        lowered = text.lower()
        if any(k in lowered for k in payment_keywords):
            continue
        text = text[:140]
        if role == "user":
            lines.append(f"U: {text}")
        elif role == "model":
            lines.append(f"A: {text}")
        if len(lines) >= 6:
            break
    if not lines:
        return None
    return " | ".join(lines)


async def _persist_session_summary(db: AsyncSession, phone: str, summary: str) -> None:
    result = await db.execute(select(ConversationState).where(ConversationState.phone == phone))
    state = result.scalar_one_or_none()
    if not state:
        return
    temp = {}
    if state.temp_data:
        try:
            parsed = json.loads(state.temp_data)
            if isinstance(parsed, dict):
                temp = parsed
        except (json.JSONDecodeError, TypeError):
            temp = {}
    temp["session_summary"] = summary
    state.temp_data = json.dumps(temp)
    db.add(state)
    await db.flush()


def _salvage_reply_from_tool(tool_name: str | None, resp: dict | None) -> str | None:
    """Build a minimal grounded reply from the last successful tool response
    when Gemini produced a tool call but emitted no follow-up text.

    Returns None when salvage is unsafe — we only surface content that is
    explicitly user-facing (server-authored `message` field, or a canonical
    success signal we know how to render). No freelancing on raw data —
    that's what hallucinates.
    """
    if not tool_name or not isinstance(resp, dict):
        return None

    # Server-authored user-facing string — always safe.
    msg = resp.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()[:1000]

    # Payment-link surface for order creation tools. These are the most
    # common turns where a silent-text failure is most painful.
    pay_url = resp.get("payment_url") or resp.get("payment_link")
    if isinstance(pay_url, str) and pay_url.startswith(("http://", "https://")):
        brand = resp.get("brand_name", "").strip()
        amount = resp.get("amount")
        tail = f" for {brand} ₹{amount}" if brand and amount else ""
        return f"Done ✓ Tap to pay{tail}: {pay_url}"

    # Batch-order result.
    if tool_name == "create_batch_gift_orders":
        ok = resp.get("successful")
        if isinstance(ok, int) and ok > 0:
            return f"Done ✓ Created {ok} order(s). You'll get payment links in the next message."

    # No safe salvage for list/search tools — the model is meant to render
    # those with context we can't reconstruct here.
    return None


async def _handle_preference_reply_if_pending(
    db: AsyncSession, phone: str, text: str
) -> str | None:
    """Intercept incoming messages from recipients who have a pending PreferenceRequest.

    Returns an ack string if the message was handled as a preference response,
    or None if the message should fall through to the normal agent pipeline.

    Logic:
    1. Check whether there is a pending PreferenceRequest for this phone.
    2. If yes, route the message text to save_recipient_preferences().
    3. Return a thank-you ack so the recipient knows we received their reply.
    4. The normal AI agent is bypassed for this turn — the recipient's message
       is a structured preference response, not a gifting conversation.

    Edge cases handled:
    - No pending request → return None (fall through to normal agent).
    - Expired requests are skipped (save_recipient_preferences handles all pending
      rows for the phone, not just the one that was "current").
    - Crashes in the preference saver are logged and fall through.
    - If preference_interceptor_enabled is False, return None (fall through to agent).
    """
    # Feature flag: preference interceptor
    if not settings.preference_interceptor_enabled:
        return None

    from app.models.recipient_preference import PreferenceRequest
    from app.bot.tools.tool_impl.recipient_preferences import save_recipient_preferences

    pending = await db.scalar(
        select(PreferenceRequest).where(
            PreferenceRequest.recipient_phone == phone,
            PreferenceRequest.status == "pending",
        )
    )
    if pending is None:
        return None

    logger.info(
        "[%s] preference reply intercepted — routing to save_recipient_preferences",
        phone,
    )
    try:
        result = await save_recipient_preferences(
            db=db,
            recipient_phone=phone,
            raw_response=text,
            request_id=pending.id,
        )
        await db.commit()
        logger.info(
            "[%s] preference reply saved pref_id=%s notified=%d",
            phone, result.get("preference_id"), result.get("notified_count", 0),
        )
    except Exception:
        logger.exception("[%s] save_recipient_preferences failed — falling through", phone)
        return None

    # Friendly ack to the recipient — warm, does not reveal who the gift-giver is.
    cats = result.get("parsed", {}).get("preferred_categories") or ""
    if cats:
        return (
            f"Thanks for sharing! We've noted that you like: {cats}.\n\n"
            "Your gift preferences have been saved. The person gifting you will "
            "be notified with your choices."
        )
    return (
        "Thanks for sharing your preferences! "
        "The person gifting you will be notified with what you've shared."
    )


async def _log_llm_usage(
    db: AsyncSession,
    phone: str,
    model: str,
    purpose: str,
    phase: Optional[str],
    session_id: Optional[str],
    usage_payload: Optional[dict],
    latency_ms: float,
    success: bool = True,
    error_type: Optional[str] = None,
    fallback_used: bool = False,
    tool_calls_count: int = 0,
) -> None:
    """Log LLM usage to database for cost tracking."""
    try:
        phone_hash = hash_phone(phone)
        input_tokens = usage_payload.get("prompt_token_count") if usage_payload else None
        output_tokens = usage_payload.get("candidates_token_count") if usage_payload else None
        cache_hit_tokens = usage_payload.get("cached_content_token_count") if usage_payload else None
        total_tokens = usage_payload.get("total_token_count") if usage_payload else None

        estimated_cost = estimate_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
        )

        event = LlmUsageEvent(
            id=generate_id("llu"),
            provider="google_genai" if "gemini" in model.lower() else "deepseek",
            model=model,
            purpose=purpose,
            phase=phase,
            conversation_id=session_id,
            phone_hash=phone_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            latency_ms=int(latency_ms),
            success=success,
            error_type=error_type,
            fallback_used=fallback_used,
            tool_calls_count=tool_calls_count if tool_calls_count > 0 else None,
        )
        db.add(event)
        await db.flush()
        logger.info(
            "[%s] llm_usage_logged | %s:%s | cost=$%.6f | tokens=%s",
            mask_phone(phone), purpose, model, float(estimated_cost),
            total_tokens or "unknown",
        )
    except Exception as e:
        logger.exception("[%s] failed to log llm_usage_event: %s", phone, e)


async def handle_message(db: AsyncSession, phone: str, text: str) -> str:
    """Process an incoming WhatsApp message and return the bot reply.

    Uses Google ADK for orchestration — no manual turn management needed.
    ADK handles:
    - Turn alternation (user/model)
    - Tool execution loop
    - Function call batching
    - Session/history management
    """
    # Rate limit BEFORE any downstream work — cheapest possible rejection.
    rl = await check_and_bump(phone)
    if not rl.allowed:
        logger.info("[%s] rate-limited, retry_after=%.1fs", phone, rl.retry_after_seconds)
        return "You're going a bit fast 🙏 Give me a few seconds and try again."

    # ── Preference reply interceptor ───────────────────────────────────────────
    # If this phone has an active pending PreferenceRequest, treat the message
    # as a preference response instead of routing it through the AI agent.
    # This must run before the product-URL fast path and the classifier so a
    # recipient's "I like gadgets" is never misrouted as a wishlist save or
    # a normal gifting conversation.
    try:
        pref_reply = await _handle_preference_reply_if_pending(db, phone, text)
        if pref_reply is not None:
            return pref_reply
    except Exception:
        logger.exception("[%s] preference reply interceptor crashed — falling through", phone)
    # ──────────────────────────────────────────────────────────────────────────

    # Set request context for tools (context vars are async-safe)
    token_db = token_phone = token_produced = None
    try:
        token_db, token_phone, token_produced = set_context(db, phone)

        # Load user context (user row + friend-flow hint + cart size)
        user, celebrant_occasion_id, cart_count = await _load_user_context(db, phone)

        # Limited-launch cap: an unregistered phone is turned away with a fixed
        # waitlist reply BEFORE any classifier/LLM work, so waitlisted users
        # cost zero tokens. Existing users (user is not None) always pass; so do
        # recipients, who were already handled by the preference interceptor
        # above. The COUNT only runs for unregistered phones — registered users
        # skip it entirely. cap <= 0 disables the gate.
        if user is None and settings.launch_user_cap > 0:
            user_count = await db.scalar(select(func.count()).select_from(User))
            if (user_count or 0) >= settings.launch_user_cap:
                logger.info("[%s] launch cap reached (%s) — waitlisted", phone, user_count)
                return settings.waitlist_message

        # Product URLs first — never block a wishlist save behind intent classify
        # (which may call Gemini) or the full ADK stack. See `action_router` docstring.
        # Pass celebrant context so a friend-flow paste is saved as a gift idea
        # against that occasion instead of polluting the user's personal wishlist.
        link_reply = await try_handle_product_urls_before_classifier(
            db, phone, text,
            celebrant_occasion_id=celebrant_occasion_id,
        )
        if link_reply is not None:
            return link_reply

        # Capture any budget mention before classify so it survives across
        # turns. Cheap text scan, no LLM. Does nothing when no signal found.
        from app.bot.state_utils import remember_budget
        try:
            await remember_budget(db, phone, text)
        except Exception:
            logger.exception("[%s] budget capture crashed — ignoring", phone)

        try:
            from app.bot.state_utils import get_state, has_active_gift_context, update_active_gift_context
            state = await get_state(db, phone)
            if has_active_gift_context(state):
                gift_updates = _active_gift_context_updates_from_text(text)
                if gift_updates:
                    await update_active_gift_context(db, phone, gift_updates)
        except Exception:
            logger.exception("[%s] active gift context capture crashed — ignoring", phone)

        # Double-safety: if a product URL slipped past the preflight above
        # (shouldn't happen), forward it before paying for classify.
        urls = extract_urls(text)
        product_urls = [u for u in urls if is_product_link(u)]
        if product_urls:
            safety = await try_handle_product_urls_before_classifier(
                db, phone, text,
                celebrant_occasion_id=celebrant_occasion_id,
            )
            if safety is not None:
                return safety

        # Exact whole-message lookups ("menu", "cart", "my orders") are answered
        # before the router runs. This is a lookup table, not intent inference —
        # a message that is exactly "menu" has no other reading — so paying
        # ~1.9s of DeepSeek to be told "other, no tools" is pure waste. Anything
        # longer or ambiguous falls through to the router below.
        exact_reply = await try_handle_exact_phrases(db, phone, text, user=user)
        if exact_reply is not None:
            return exact_reply

        # --- Intent routing (pre-flight) --------------------------------------
        # One history-aware LLM call decides what the user wants and which
        # tools are likely needed. It reads the last few turns, so a reply to
        # our own question ("Myra - birthday - 7th Aug") is understood as the
        # ANSWER rather than mistaken for a new request — the failure that lost
        # six occasions in the 2026-08 transcript.
        #
        # Fail-open: on timeout/error this returns `DEGRADED` (intent="other",
        # no tools), which narrows nothing, so Gemini gets the full tool set
        # plus full history and still handles the turn.
        intent_result = await route_intent(db, phone, text)
        logger.info("[%s] intent=%s conf=%.2f src=%s tools=%s",
                    phone, intent_result.intent, intent_result.confidence,
                    intent_result.source, list(intent_result.likely_tools))

        # Deterministic action router — handles menu, product gate, wishlist
        # link save, and "yes-while-awaiting-link" without ever calling the LLM.
        # If it returns a string, we're done.
        router_reply = await try_handle_actions(
            db, phone, text, user=user, intent_result=intent_result,
        )
        if router_reply is not None:
            return router_reply

        # Serialize concurrent turns for the same phone. Two layers:
        #   1. Per-phone asyncio.Lock — cheap, in-process, covers the common
        #      case of two coroutines in one uvicorn worker.
        #   2. Postgres advisory lock on the same phone — covers the
        #      multi-worker case where Meta retries land on a different replica.
        # Order matters: in-process first so single-worker fast path never
        # pays the DB round-trip.
        phone_lock = await _acquire_phone_lock(phone)
        _gc_phone_locks()
        async with phone_lock, _db_phone_lock(db, phone):
            # Get or create session (inside the lock — session mutation must
            # be serialized per phone).
            session, _is_new_session = await _get_or_create_session(phone)

            # Scrub old tool-crash events from replayed history. Prevents a
            # poisoned session from convincing Gemini that a recently-fixed
            # tool still fails — which would cause ``tools=NONE`` turns
            # indefinitely after a deploy that fixed the underlying bug.
            # See issue #009 §5b Gap G6 (session poisoning postmortem).
            scrubbed = _scrub_poisoned_events(session)
            if scrubbed:
                metrics.inc("poisoned_events_scrubbed")
                logger.info(
                    "[%s] scrubbed %d poisoned events from session %s",
                    phone, scrubbed, session.id,
                )

            # Build the per-turn context (today + user + intent hint) and
            # prepend it to the USER MESSAGE rather than injecting through
            # session.state. This eliminates cross-turn state races and keeps
            # STATIC_SYSTEM_PROMPT byte-stable for Gemini implicit caching.
            dynamic_tail = await _build_dynamic_tail(db, phone, user, celebrant_occasion_id)
            intent_hint = _intent_hint(intent_result, session)
            route_hint = _route_hint(intent_result)
            preface = (
                f"[turn context]\n{dynamic_tail}{intent_hint}{route_hint}\n"
                f"[/turn context]\n\n"
            )

            # Safe greeting short-circuit: exact greeting in brand-new sessions
            # only. If context is already active, continue with the normal flow.
            norm_text = (text or "").strip().lower()
            has_active_context = bool(celebrant_occasion_id) or (cart_count > 0)
            # Not intent inference: an exact "hi"/"hello" opening a brand-new
            # session with no active context has exactly one sensible reply, so
            # we skip the LLM entirely. Deliberately does NOT consult the intent
            # router — the router may legitimately label a bare "Hi" as `other`,
            # and a cold-start greeting must not depend on a network call.
            if (
                _is_new_session
                and norm_text in _SAFE_GREETING_EXACTS
                and not has_active_context
            ):
                campaign_template = None
                if user and user.name:
                    greeting_reply = (
                        f"Welcome back, {user.name} 👋\n\n"
                        "What would you like to do today?\n\n"
                        "1. Save an occasion\n"
                        "2. View upcoming reminders\n"
                        "3. Ask someone's gift preferences\n"
                        "4. Avoid duplicate gifts"
                    )
                else:
                    # Message-match: if this phone has a fresh, unconsumed
                    # marketing-template send on record, greet with copy
                    # tied to that template's hook instead of the generic
                    # default. Consumes the row either way — a campaign
                    # context only colors the very first reply.
                    campaign_template = await _load_and_consume_campaign_context(db, phone)
                    greeting_reply = acquisition_greeting_for(campaign_template)
                await _save_message(db, phone, MessageRole.USER, content=text)
                await _save_message(db, phone, MessageRole.MODEL, content=greeting_reply)
                logger.info(
                    "[%s] safe_greeting_shortcut new_session=%s text=%s campaign_template=%s",
                    phone, _is_new_session, norm_text, campaign_template,
                )
                metrics.inc("greeting_shortcut")
                return greeting_reply

            # Pick the phase-specific Runner (tool surface scoped to the active
            # flow). All phases share the same session_service, so history is
            # continuous across phase transitions within a conversation.
            phase = _pick_phase(user, celebrant_occasion_id, cart_count)
            critical_turn = _is_critical_turn(text, phase, cart_count)
            selected_tools, subflow = _select_tools_for_turn(phase, text, intent_result)
            spent_today, daily_budget, budget_usage_pct = await get_daily_budget_usage(db)
            policy_model = settings.gemini_model
            policy_block_noncritical = False
            if not critical_turn and budget_usage_pct >= 90.0:
                policy_block_noncritical = True
            elif not critical_turn and budget_usage_pct >= 70.0:
                policy_model = settings.gemini_fast_model

            runner = _get_runner_for_phase(phase, policy_model, selected_tools)
            logger.info(
                "[%s] phase=%s subflow=%s tools=%d cart=%d model=%s budget=%.1f%% ($%.4f/$%.2f)",
                phone, phase, subflow, len(selected_tools), cart_count, policy_model,
                budget_usage_pct, float(spent_today), float(daily_budget),
            )

            # Create user message — dynamic context is part of the message body so
            # the agent sees it as turn-scoped, not as shared session state.
            new_message = types.Content(
                role="user",
                parts=[types.Part(text=preface + text)],
            )

            # Save user message to DB (for our records)
            await _save_message(db, phone, MessageRole.USER, content=text)

            # Run agent — ADK handles everything. Hard deadline guards against stuck
            # Gemini calls hanging the webhook caller.
            final_response = None
            tool_calls_seen: list[str] = []
            tool_errors_seen: list[str] = []
            # Track the last successful tool response so we can salvage a
            # minimal grounded reply when Gemini emits a tool_call but no
            # follow-up text (a known single-tool-turn edge case in ADK).
            last_tool_name: str | None = None
            last_tool_resp: dict | None = None
            # Server-authored reply from a failed tool, used verbatim instead of
            # paying a second full-prompt call for the model to rephrase it.
            tool_user_message: str | None = None
            last_usage_metadata: dict[str, int] | None = None
            fallback_reason: str | None = None
            logger.info("[%s] turn start | user_msg=%r", phone, _preview_text(text))

            async def _consume_events() -> None:
                nonlocal final_response, last_usage_metadata, last_tool_name, last_tool_resp, fallback_reason
                nonlocal tool_user_message
                max_tool_rounds = settings.agent_max_tool_rounds
                tool_call_count = 0

                async for event in runner.run_async(
                    user_id=phone,
                    session_id=session.id,
                    new_message=new_message,
                ):
                    # Enforce tool-call loop cap
                    if tool_call_count > max_tool_rounds:
                        logger.warning(
                            "[%s] tool_loop_limit_exceeded: %d > %d — stopping loop",
                            phone, tool_call_count, max_tool_rounds,
                        )
                        fallback_reason = "tool_loop_limit_exceeded"
                        break

                    usage_payload = _usage_metadata_payload(event)
                    if usage_payload:
                        last_usage_metadata = usage_payload
                        logger.info(
                            "[%s] gemini_usage | prompt=%s candidates=%s total=%s cached=%s",
                            mask_phone(phone),
                            usage_payload.get("prompt_token_count"),
                            usage_payload.get("candidates_token_count"),
                            usage_payload.get("total_token_count"),
                            usage_payload.get("cached_content_token_count"),
                            extra={
                                "type": "gemini_usage",
                                "phone": mask_phone(phone),
                                "phase": phase,
                                **usage_payload,
                            },
                        )
                    try:
                        fn_calls = event.get_function_calls() if hasattr(event, 'get_function_calls') else []
                        for fc in fn_calls or []:
                            tool_calls_seen.append(fc.name)
                            tool_call_count += 1
                            logger.info("[%s] tool_call | %s args=%s", phone, fc.name, _preview_text(fc.args, 300))
                        fn_resps = event.get_function_responses() if hasattr(event, 'get_function_responses') else []
                        for fr in fn_resps or []:
                            resp = fr.response or {}
                            if isinstance(resp, dict) and resp.get("error"):
                                tool_errors_seen.append(f"{fr.name}:{resp.get('error')}")
                                # A tool that failed with a server-authored
                                # `user_message` has already written the exact
                                # reply the user should see. Letting the model
                                # rephrase it costs a second full-prompt call
                                # (~12k tokens, ~2s) to say the same thing, so
                                # take it verbatim and stop the turn.
                                direct = resp.get("user_message")
                                if isinstance(direct, str) and direct.strip():
                                    tool_user_message = direct.strip()[:1000]
                            else:
                                last_tool_name = fr.name
                                last_tool_resp = resp if isinstance(resp, dict) else None
                            logger.info("[%s] tool_resp | %s -> %s", phone, fr.name, _preview_text(resp, 300))
                    except Exception as log_e:
                        logger.warning("[%s] tool audit failed: %s", phone, log_e)

                    # Stop before ADK sends the tool result back to Gemini. The
                    # failed tool already supplied the user-facing reply, so the
                    # extra round-trip would only restate it.
                    if tool_user_message and fallback_reason != "tool_user_message":
                        # _end_invocation_on_user_message has told ADK to stop, so
                        # the generator winds down without a second LLM call. No
                        # break here: tearing the generator down mid-iteration is
                        # what produced the OTel "Failed to detach context" noise.
                        fallback_reason = "tool_user_message"
                        logger.info(
                            "[%s] short-circuit on tool user_message (%s)",
                            mask_phone(phone), ",".join(tool_errors_seen[-1:]),
                        )

                    # Only capture events that actually carry assistant text.
                    # function_response events have `content` but no text parts;
                    # without this guard they overwrite the real reply and the
                    # turn falls through to the empty-reply fallback below.
                    if event.content and any(
                        getattr(p, 'text', None) for p in event.content.parts
                    ):
                        final_response = event.content

            gemini_start = time.time()

            # Check budget guard before Gemini call
            phone_hash = hash_phone(phone)
            budget_ok, budget_violation = await check_all_budgets(db, phone_hash)

            breaker_open = not _gemini_breaker.allow()
            if breaker_open:
                metrics.inc("gemini_circuit_open_skip")
                fallback_reason = "breaker_open"
                logger.warning(
                    "[%s] gemini circuit OPEN — skipping LLM call", mask_phone(phone),
                )
                final_response = None
            elif policy_block_noncritical:
                metrics.inc("budget_policy_noncritical_blocked")
                fallback_reason = "budget_policy_noncritical_blocked"
                logger.warning(
                    "[%s] LLM blocked by budget policy (usage=%.1f%%, non-critical turn)",
                    mask_phone(phone), budget_usage_pct,
                )
                final_response = None
            elif not budget_ok:
                metrics.inc("budget_limit_exceeded")
                fallback_reason = f"budget_limit_{budget_violation}"
                logger.warning(
                    "[%s] LLM budget limit exceeded (%s) — skipping call",
                    mask_phone(phone), budget_violation,
                )
                final_response = None
            else:
                metrics.inc("gemini_calls")
                try:
                    await asyncio.wait_for(
                        _consume_events(), timeout=AGENT_TURN_TIMEOUT_SECONDS,
                    )
                    if final_response is None and not tool_calls_seen:
                        # Gemini 2.5 flash occasionally returns finish_reason=STOP
                        # with an empty candidate (no text, no tool call) on
                        # large tool-calling prompts. One retry almost always
                        # recovers it; the empty-reply fallback below remains
                        # the net if the retry comes back empty too.
                        # ponytail: retry re-appends new_message to the ADK
                        # session (user msg appears twice in history). Harmless
                        # and trimmed later; dedupe in-session if it ever
                        # confuses the model.
                        metrics.inc("gemini_empty_retry")
                        logger.warning(
                            "[%s] gemini returned empty candidate — retrying once",
                            mask_phone(phone),
                        )
                        await asyncio.wait_for(
                            _consume_events(), timeout=AGENT_TURN_TIMEOUT_SECONDS,
                        )
                    _gemini_breaker.record_success()
                except asyncio.TimeoutError:
                    _gemini_breaker.record_failure()
                    metrics.inc("gemini_timeouts")
                    fallback_reason = "timeout"
                    logger.warning(
                        "[%s] ADK run timed out after %.1fs",
                        mask_phone(phone), AGENT_TURN_TIMEOUT_SECONDS,
                    )
                    final_response = None
                except ValueError as e:
                    # ADK raises ``ValueError: Function X is not found in the
                    # tools_dict.`` when Gemini hallucinates a tool name that
                    # isn't declared for the current phase. This is not a
                    # network failure and should not trip the breaker, nor
                    # should it crash the whole turn into the broad "stuck"
                    # fallback. Log it, mark the turn for salvage, and let
                    # downstream rendering emit a graceful message.
                    msg_lc = str(e).lower()
                    if "not found in the tools_dict" in msg_lc:
                        metrics.inc("tool_not_in_phase")
                        fallback_reason = "tool_not_in_phase"
                        logger.warning(
                            "[%s] ADK attempted unregistered tool in phase '%s': %s",
                            mask_phone(phone), phase, e,
                        )
                        final_response = None
                    else:
                        metrics.inc("non_gemini_failures")
                        raise
                except Exception as e:
                    # Only count true Gemini/network failures — swallowing tool
                    # or DB exceptions here would falsely trip the breaker.
                    if _is_gemini_error(e):
                        _gemini_breaker.record_failure()
                        metrics.inc("gemini_failures")
                        # Transient upstream 5xx / 429 — degrade to the polite
                        # fallback instead of bubbling a ServerError to Sentry.
                        # The breaker still records the failure above, so a
                        # sustained outage will trip it on the 5th hit.
                        if isinstance(e, genai_errors.ServerError) or (
                            isinstance(e, genai_errors.APIError)
                            and getattr(e, "code", None) in (429, 503)
                        ):
                            fallback_reason = "gemini_5xx"
                            logger.warning(
                                "[%s] gemini transient error %s — serving fallback",
                                mask_phone(phone), type(e).__name__,
                            )
                            final_response = None
                        else:
                            raise
                    else:
                        metrics.inc("non_gemini_failures")
                        raise
                finally:
                    gemini_ms = (time.time() - gemini_start) * 1000
                    latency_extra = {
                        "type": "gemini",
                        "phone": mask_phone(phone),
                        "duration_ms": gemini_ms,
                        "bucket": latency_bucket(gemini_ms),
                        "breaker": _gemini_breaker.state,
                    }
                    if last_usage_metadata:
                        _record_gemini_usage_metrics(phase, last_usage_metadata)
                        latency_extra.update(last_usage_metadata)
                    logger.info(
                        "latency",
                        extra=latency_extra,
                    )

                    # Log LLM usage for cost tracking
                    try:
                        error_type = None
                        if fallback_reason and fallback_reason.startswith("budget_limit"):
                            error_type = "budget_limit_exceeded"
                        elif fallback_reason == "tool_loop_limit_exceeded":
                            error_type = "tool_loop_limit_exceeded"
                        elif fallback_reason == "timeout":
                            error_type = "timeout"
                        elif fallback_reason == "tool_not_in_phase":
                            error_type = "tool_not_in_phase"

                        await _log_llm_usage(
                            db=db,
                            phone=phone,
                            model=settings.gemini_model,
                            purpose="main_agent",
                            phase=phase,
                            session_id=session.id,
                            usage_payload=last_usage_metadata,
                            latency_ms=gemini_ms,
                            success=(not error_type and fallback_reason != "timeout"),
                            error_type=error_type,
                            fallback_used=(fallback_reason is not None),
                            tool_calls_count=len(tool_calls_seen),
                        )
                    except Exception as log_e:
                        logger.exception("[%s] failed to log usage: %s", phone, log_e)


            # Extract text from response
            reply = ""
            if final_response:
                for part in final_response.parts:
                    if hasattr(part, 'text') and part.text:
                        reply += part.text

            if not reply and tool_user_message:
                # Short-circuited above: the tool's own wording is the reply.
                reply = tool_user_message

            if not reply:
                # When the circuit broke we never called Gemini — give the user a
                # clear "try again" message instead of the generic greet-fallback.
                if breaker_open or fallback_reason == "gemini_5xx":
                    reply = _GEMINI_OUTAGE_FALLBACK
                elif fallback_reason == "budget_policy_noncritical_blocked":
                    reply = (
                        "I can still help with quick essentials right now. "
                        "Please try that request again shortly."
                    )
                elif fallback_reason and fallback_reason.startswith("budget_limit"):
                    reply = "I'm temporarily rate-limited on requests. Please try again in a moment. 🙏"
                else:
                    salvaged = _salvage_reply_from_tool(last_tool_name, last_tool_resp)
                    if salvaged:
                        reply = salvaged
                        fallback_reason = "tool_salvage"
                        logger.info(
                            "[%s] salvaged reply from last tool (%s)",
                            mask_phone(phone), last_tool_name,
                        )
                    else:
                        # ADK returned empty — soft fallback. Don't reset to a
                        # cold-start greeting: that wipes the user's mental
                        # model of the conversation ("did it forget me?") even
                        # when we're mid-flow. Use a recoverable nudge so they
                        # can resend or rephrase without feeling logged out.
                        # Varied wording: the outbound dedup suppresses an
                        # identical (phone, body) within 60s, so a static
                        # fallback sent twice in a row leaves the user with
                        # total silence on the second message.
                        _variants = (
                            "Hmm, didn't quite catch that 🙈 — could you say "
                            "it again, or rephrase? If you were mid-flow, "
                            "just resend your last message.",
                            "Sorry, that one slipped past me 😅 — mind "
                            "sending it again or wording it differently?",
                            "Oops, I lost that one 🙈 — please resend or "
                            "rephrase and I'll pick it right up.",
                        )
                        # Rotate per-process so consecutive fallbacks always
                        # differ and the outbound dedup never silences the
                        # user (random.choice would repeat 1-in-3).
                        # ponytail: per-worker counter; two workers could
                        # emit the same variant — dedup then correctly drops
                        # one identical nudge, acceptable.
                        reply = _variants[
                            next(_EMPTY_FALLBACK_COUNTER) % len(_variants)
                        ]
                        if fallback_reason is None:
                            fallback_reason = "empty_reply"

            # Structured turn summary for eval / regression analysis. Logs
            # tools_called + fallback_reason so hallucinations vs canned-fallbacks
            # are distinguishable in production logs.
            logger.info(
                "turn_summary",
                extra={
                    "type": "turn",
                    "phone": mask_phone(phone),
                    "tools_called": tool_calls_seen,
                    "tool_errors": tool_errors_seen,
                    "fallback_reason": fallback_reason,
                    "reply_len": len(reply),
                    "breaker": _gemini_breaker.state,
                },
            )
            logger.info(
                "[%s] turn end | tools=%s fallback=%s reply=%r",
                phone, tool_calls_seen or "NONE", fallback_reason or "-",
                _preview_text(reply),
            )

            # Save bot response to DB (for our records)
            await _save_message(db, phone, MessageRole.MODEL, content=reply)

            summary = _safe_compact_session_summary(session)
            if summary:
                await _persist_session_summary(db, phone, summary)

            # Trim session to prevent unbounded token growth
            await _trim_session_events(phone, session.id)

            return reply

    except Exception as e:
        logger.error("Agent error for %s: %s", phone, e, exc_info=True)
        # Ship to Sentry — the broad except keeps the user-facing fallback
        # working, but silent failures here are how real regressions
        # hide. See issue #009 G4 precedent: a truncated reply went
        # unexplained for hours because the error never left the process.
        try:
            from app.core.observability import capture_exception as _sentry_capture
            _sentry_capture(e, phone=mask_phone(phone), user_msg=_preview_text(text))
        except Exception:
            pass
        fallback = "Hmm, I got a bit stuck on that. Could you rephrase, or type *menu* for options? 🙏"
        # Keep user/assistant pairing consistent even when the agent fails.
        try:
            await _save_message(db, phone, MessageRole.MODEL, content=fallback)
        except Exception:
            logger.exception("Failed to persist fallback reply for %s", phone)
        return fallback
    finally:
        # Reset context vars
        if token_db is not None and token_phone is not None and token_produced is not None:
            reset_context(token_db, token_phone, token_produced)


async def _save_message(
    db: AsyncSession, phone: str, role: MessageRole,
    content: str | None = None,
) -> None:
    """Persist a message row (for our records, not ADK's session)."""
    msg = ConversationMessage(
        phone=phone,
        role=role,
        content=content,
    )
    db.add(msg)
    await db.flush()
