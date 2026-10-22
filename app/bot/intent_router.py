"""History-aware intent routing.

Replaces the phrase-list/keyword approach in `intent_classifier.py`. One LLM
call reads the last few turns and returns what the user wants plus which tools
are likely needed, so new phrasings are *understood* rather than pattern-matched.

Why this exists (2026-08 post-mortem, "DJ" transcript): the old classifier's
prompt was literally ``Message: {msg}`` — no history — so a reply to our own
question ("Myra - birthday - 7th Aug") was indistinguishable from a request to
list birthdays, and the deterministic router answered "I don't have any
occasions saved for you yet" while occasions existed. Six occasions were lost
across eleven turns. No keyword could have fixed that; the classifier could not
see that we had just asked a question.

Design rules:
  * The tool catalogue in the prompt is GENERATED from the tool registry
    (`TOOL_SPECS`) — adding a tool makes it routable with no edit here.
  * Fail open. Any timeout/error returns a `degraded` result and the caller
    hands the full history to Gemini, which has every tool and can infer intent
    itself. A model outage must never become a product outage.
  * This module decides *routing hints only*. It never writes to the database
    and never produces user-visible text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ConversationMessage, MessageRole
from app.utils.phones import phone_aliases

logger = logging.getLogger("wishkr.intent_router")

# Last N turns handed to the router. Enough to see our own question plus the
# user's answer; small enough to stay cheap and fast on the hot path.
MAX_CONTEXT_MESSAGES = 5
_MAX_MESSAGE_CHARS = 300

# Per-operation httpx timeout. NOTE: httpx applies this to connect/read/write
# SEPARATELY, so it is NOT a wall-clock bound — a hung endpoint can burn
# several multiples of it. `_TOTAL_BUDGET_SECONDS` is the real guarantee.
_TIMEOUT_SECONDS = 3.0

# Hard wall-clock ceiling for the whole routing step, DB read included. This
# runs on the WhatsApp reply path, so a sick upstream must cost us this much
# and no more: past it we degrade and let Gemini handle the turn with full
# history. Measured 2026-08: with only the httpx timeout, an unreachable
# DeepSeek stalled the turn ~8s.
_TOTAL_BUDGET_SECONDS = 4.0

# PII scrubbing before anything leaves for a third-party API. Mirrors
# `app/services/gift_refinement.py` — intent routing needs the *shape* of the
# message, never the digits.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?91[\s-]?)?\d{10}\b")


@dataclass(frozen=True)
class RouteResult:
    """Routing hints for one turn.

    Superset of the legacy ``IntentResult`` — `.intent`, `.confidence` and
    `.source` keep the same meaning so existing consumers
    (`_select_tools_for_turn`, tests) keep working unchanged.
    """

    intent: str
    confidence: float
    source: str  # "llm" | "degraded"
    likely_tools: tuple[str, ...] = ()
    slots: dict[str, Any] = field(default_factory=dict)

    @property
    def is_degraded(self) -> bool:
        return self.source == "degraded"


# Returned when the router is unavailable. `intent="other"` is the widest
# label: it narrows nothing, so Gemini receives the full tool set and decides
# for itself. Deliberately NOT an error — see the fail-open rule above.
DEGRADED = RouteResult(intent="other", confidence=0.0, source="degraded")


def _redact(value: str) -> str:
    text = _URL_RE.sub("[link]", value or "")
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return " ".join(text.split())[:_MAX_MESSAGE_CHARS]


async def recent_messages(db: AsyncSession, phone: str) -> list[dict[str, str]]:
    """Last few user/model turns, oldest first, redacted."""
    rows = (
        await db.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.phone.in_(phone_aliases(phone)),
                ConversationMessage.role.in_([MessageRole.USER, MessageRole.MODEL]),
                ConversationMessage.content.isnot(None),
                ConversationMessage.created_at >= datetime.now(timezone.utc) - timedelta(days=1),
            )
            .order_by(
                ConversationMessage.created_at.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(MAX_CONTEXT_MESSAGES)
        )
    ).scalars().all()
    return [
        {"role": row.role.value, "content": _redact(row.content or "")}
        for row in reversed(rows)
        if row.content
    ]


@lru_cache(maxsize=1)
def _tool_catalogue() -> str:
    """Render the tool registry as prompt lines.

    Generated, never hand-written: the registry is the single source of truth,
    so a newly registered tool is routable without touching this module.
    """
    from app.bot.tools.schemas import TOOL_SPECS

    lines = []
    for name in sorted(TOOL_SPECS):
        description = TOOL_SPECS[name].declaration.description or ""
        # Tool descriptions are long multi-line prompt-engineering blocks
        # ("WHEN TO USE:\n...\nUSE WHEN:\n- ..."). Routing only needs a label,
        # and the full schemas go to Gemini anyway — so take the first
        # non-empty, non-header line and cap it. Without this the catalogue
        # alone is ~34k chars, more than the layer this module replaces.
        summary = ""
        for line in description.splitlines():
            candidate = line.strip()
            if not candidate or candidate.endswith(":"):
                continue
            summary = candidate.lstrip("- ").rstrip(".")
            break
        lines.append(f"- {name}: {summary[:100]}" if summary else f"- {name}")
    return "\n".join(lines)


# Fixed label vocabulary. Downstream code branches on these strings
# (`action_router` product gate / wishlist-link gate, `agent._intent_hint`), so
# a free-form label would leave those branches with no producer — dead code that
# still LOOKS live. Add a label here only together with its consumer.
VALID_INTENTS = frozenset({
    "greeting",
    "affirmative",
    "menu_request",
    "product_question",
    "show_wishlist",
    "show_occasions",
    "save_occasion",
    "delete_occasion",
    "gift_suggestion",
    "other",
})

_PROMPT = """You route WhatsApp messages for Wishkr, a gifting and reminder assistant.

Read the conversation in ANY language or code-switching style and decide what the user wants RIGHT NOW, then name the \
tools needed to do it.

Available tools:
{tools}

`intent` MUST be exactly one of:
- greeting: just a hello, nothing else
- affirmative: a bare yes/ok/sure answering our previous message
- menu_request: asking what you can do / for options
- product_question: asking about a specific PHYSICAL product (earbuds, shirt)
- show_wishlist: wants to see their own saved wishlist
- show_occasions: wants to see occasions they already saved
- save_occasion: wants to save/record an occasion, or is answering our question \
about one
- delete_occasion: wants to remove a saved occasion or reminder
- gift_suggestion: wants gift ideas, naming no specific product
- other: anything else

Rules:
- The LAST user message is what you are routing. Earlier turns are context.
- If the assistant just asked a question, the user's reply is almost always the \
ANSWER to it — not a new request. Example: assistant asks "What's your friend's \
name and when is their birthday?" and the user says "Myra - birthday - 7th Aug". \
That is add_occasion with those slots, NOT a request to list occasions.
- Saving and listing are different. "Save Ravi's birthday" is add_occasion. \
"What birthdays did I save?" is get_my_occasions. A message carrying a specific \
person and date is almost always a save.
- If the user names several occasions in one message, return add_occasion and put \
every one of them in slots.occasions.
- Order/payment/refund/return questions use get_order_support, including guests. Never confuse a return with a new purchase.
- Explicit language requests and ending a flow use set_chat_preferences. Name corrections use register_user.
- A multilingual confirmation is an answer to the previous question, not a greeting. Use the actual action tool when appropriate.
- Infer the requested/current reply language semantically and include its name in slots.reply_language for EVERY turn. Honor explicit language preferences from history unless the user changes them. No fixed language list.
- Do not depend on English words or translate the customer-facing reply here; the main agent writes it.
- Only name tools that appear in the list above.
- If the request is unclear, use intent "other" and leave likely_tools empty. \
Do not guess.

Return ONLY a JSON object, no markdown fence, no explanation:
{{"intent": "<short_snake_case_label>",
  "likely_tools": ["tool_name"],
  "slots": {{"reply_language": "<inferred language/style>"}},
  "confidence": 0.0-1.0}}

For occasion saves, slots should look like:
{{"occasions": [{{"contact_name": "Myra", "occasion_type": "BIRTHDAY", \
"date_day": 7, "date_month": 8}}]}}

Conversation:
{conversation}

JSON:"""


def _build_prompt(history: list[dict[str, str]], text: str) -> str:
    lines = []
    for msg in history:
        speaker = "User" if msg["role"] == MessageRole.USER.value else "Assistant"
        lines.append(f"{speaker}: {msg['content']}")
    lines.append(f"User: {_redact(text)}")
    return _PROMPT.format(tools=_tool_catalogue(), conversation="\n".join(lines))


def _parse(raw: str) -> RouteResult | None:
    """Parse the model's JSON. Tolerates a ```json fence."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    intent = str(data.get("intent") or "other").strip().lower()
    if intent not in VALID_INTENTS:
        # Coerce rather than reject: `likely_tools` still carries the useful
        # signal, and "other" narrows nothing. Logged because a recurring
        # unknown label means the prompt and the vocabulary have drifted.
        if intent:
            logger.info("intent router returned off-vocabulary label %r", intent)
        intent = "other"

    # Drop hallucinated tool names — the registry is authoritative. A model
    # that invents a tool must not make Gemini's selection smaller than the
    # truth, so an unknown name is simply ignored.
    from app.bot.tools.schemas import TOOL_SPECS

    raw_tools = data.get("likely_tools")
    tools: list[str] = []
    if isinstance(raw_tools, list):
        for item in raw_tools:
            name = str(item or "").strip()
            if name in TOOL_SPECS and name not in tools:
                tools.append(name)
            elif name:
                logger.info("intent router named unknown tool %r — ignored", name)

    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    slots = data.get("slots")
    if not isinstance(slots, dict):
        slots = {}

    return RouteResult(
        intent=intent,
        confidence=confidence,
        source="llm",
        likely_tools=tuple(tools),
        slots=slots,
    )


_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Process-wide client so TLS/connection setup is paid once, not per turn.

    Measured 2026-08: a fresh client per call made the first request ~6s while
    warm ones ran ~1.7s. That handshake cost landed on every cold turn.
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=_TIMEOUT_SECONDS,
                    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
                )
    return _client


async def aclose() -> None:
    """Close the shared client. Call from app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _call_model(db: AsyncSession, phone: str, text: str) -> tuple[str, dict]:
    """Read history, ask the model, return (raw content, usage dict)."""
    history = await recent_messages(db, phone)
    prompt = _build_prompt(history, text)

    client = await _get_client()
    resp = await client.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        json={
            "model": settings.deepseek_classifier_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            # Enough for several occasions' worth of slots. Too low and the
            # model hits `finish_reason: length` mid-JSON and we degrade
            # for no reason.
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    return (
        payload["choices"][0]["message"]["content"],
        payload.get("usage") or {},
    )


def _log_usage(usage: dict, latency_ms: float, *, success: bool) -> None:
    """Record this routing call's spend against the budget guard.

    Not optional: `utils/budget_guard.py` enforces the daily and monthly caps
    by SUMMING `llm_usage_events`. A call that writes no row is spend the guard
    cannot see, so the router would silently erode the very budget it is
    supposed to fit inside. Uses the provider's REPORTED token counts, not an
    estimate, so the numbers are real.

    Fire-and-forget on its own session — a logging failure must never fail a
    turn, and the caller's session may be mid-transaction.
    """
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    if not input_tokens and not output_tokens:
        return

    async def _write() -> None:
        try:
            from app.models.llm_usage_event import LlmUsageEvent
            from app.utils.id_gen import generate_id
            from app.utils.llm_cost import estimate_cost

            # Reuse the app's session factory rather than building a fresh
            # engine per call — one engine per routed turn would leak pools
            # under load.
            from app.db.session import async_session

            async with async_session() as session:
                session.add(LlmUsageEvent(
                    id=generate_id("llu"),
                    provider="deepseek",
                    model=settings.deepseek_classifier_model,
                    purpose="intent_router",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    estimated_cost_usd=estimate_cost(
                        model=settings.deepseek_classifier_model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    latency_ms=int(latency_ms),
                    success=success,
                    fallback_used=False,
                ))
                await session.commit()
        except Exception:
            logger.exception("intent router usage logging failed")

    task = asyncio.create_task(_write())

    def _report(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            logger.warning("intent router usage task failed: %s", t.exception())

    task.add_done_callback(_report)


async def route(db: AsyncSession, phone: str, text: str) -> RouteResult:
    """Return routing hints for this turn. Never raises — degrades instead."""
    if not settings.deepseek_api_key:
        logger.debug("intent router: no deepseek key configured — degraded")
        return DEGRADED

    start = time.time()
    try:
        # asyncio.wait_for is the wall-clock guarantee; the httpx timeout only
        # bounds each individual socket operation.
        raw, usage = await asyncio.wait_for(
            _call_model(db, phone, text), timeout=_TOTAL_BUDGET_SECONDS
        )
        # Log before parsing: the tokens were spent either way, and a
        # malformed response must still count against the budget.
        _log_usage(usage, (time.time() - start) * 1000, success=True)

        result = _parse(raw)
        if result is None:
            logger.warning("intent router returned unparseable JSON: %r", raw[:200])
            return DEGRADED

        logger.info(
            "[%s] route intent=%s tools=%s conf=%.2f %.0fms",
            phone, result.intent, list(result.likely_tools), result.confidence,
            (time.time() - start) * 1000,
        )
        return result

    except (asyncio.TimeoutError, httpx.TimeoutException):
        logger.warning(
            "[%s] intent router timed out after %.0fms — degrading to full tool set",
            phone, (time.time() - start) * 1000,
        )
        return DEGRADED
    except Exception as exc:
        # Fail open on ANYTHING. Gemini still has full history and every tool.
        logger.warning("[%s] intent router failed (%s) — degrading", phone, exc)
        return DEGRADED
