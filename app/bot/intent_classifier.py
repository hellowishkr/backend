"""Intent classifier: a cheap pre-flight before the main ADK agent.

Two-stage design:
1. Rule-based fast path (free, ~0ms) for unambiguous cases.
2. LLM fallback (Gemini Flash-Lite by default, ~100ms, low cost) for ambiguous inputs.

Intents:
- greeting         — hi / hello / yo / single char / emoji-only
- menu_request     — user typed "menu" (variants)
- affirmative      — plain yes-signal (yes/yess/haan/ok/sure/yep/y)
- product_question — asking for a specific physical product (earbuds, phone, book, shirt...)
- show_occasions  — user asks to list saved occasions
- other            — anything else; falls through to the main agent

Only `menu_request` and `product_question` short-circuit the main agent
(see canned_responses). Other intents are passed as HINTS in the dynamic
context so the main agent handles state-dependent cases (e.g. affirmatives
that need conversation history to resolve).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from google import genai
from google.genai import types as genai_types

from app.core.config import settings
from app.bot.language_map import (
    AFFIRMATIVE_WORDS,
    GREETING_WORDS,
    MENU_WORDS,
    PHYSICAL_PRODUCT_KEYWORDS,
    SHOW_WISHLIST_PHRASES,
    contains_product_keyword,
    looks_like_occasion_list_request,
)
from app.utils.llm_cost import estimate_cost
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.intent")


async def _async_log_classifier_usage(
    model: str,
    provider: str,
    latency_ms: float,
    input_tokens: int = 133,  # rough estimate from classifier prompt
    output_tokens: int = 2,   # maximum 8 tokens, typically much less
) -> None:
    """Log classifier usage. Fire-and-forget task.

    This function is spawned as an asyncio.create_task() so it doesn't block
    the caller. Any exceptions are logged but don't interrupt the main flow.
    This is best-effort logging: if the database is unavailable, the request
    still succeeds (the user gets a classifier result) but the usage isn't recorded.
    """
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker as async_sessionmaker
        from app.models.llm_usage_event import LlmUsageEvent
        from app.db.session import create_resilient_async_engine

        # Use the configured DB URL to create a one-off connection for logging
        engine = create_resilient_async_engine(settings.database_url)
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        try:
            async with async_session() as session:
                estimated_cost = estimate_cost(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

                event = LlmUsageEvent(
                    id=generate_id("llu"),
                    provider=provider,
                    model=model,
                    purpose="classifier",
                    phase=None,
                    conversation_id=None,
                    phone_hash=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_hit_tokens=None,
                    total_tokens=input_tokens + output_tokens,
                    estimated_cost_usd=estimated_cost,
                    latency_ms=int(latency_ms),
                    success=True,
                    error_type=None,
                    fallback_used=False,
                    tool_calls_count=None,
                )
                session.add(event)
                await session.commit()
        finally:
            await engine.dispose()
    except Exception as e:
        logger.exception("failed to log classifier usage: %s", e)

Intent = Literal[
    "greeting", "menu_request", "affirmative", "product_question",
    "show_wishlist", "show_occasions", "other",
]


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float  # 0.0–1.0
    source: Literal["rule", "llm"]


# --- Rule-based fast path ---------------------------------------------------

_AFFIRMATIVE_WORDS = AFFIRMATIVE_WORDS
_GREETING_WORDS = GREETING_WORDS
_MENU_WORDS = MENU_WORDS
_SHOW_WISHLIST_PHRASES = SHOW_WISHLIST_PHRASES
_PHYSICAL_PRODUCT_KEYWORDS = PHYSICAL_PRODUCT_KEYWORDS

_EMOJI_ONLY_RE = re.compile(
    r"^[\s"
    r"\U0001F300-\U0001FAFF"
    r"\U00002600-\U000027BF"
    r"\U0001F600-\U0001F64F"
    r"]+$"
)


def _normalize(text: str) -> str:
    return (text or "").strip().lower()


def _rule_based(text: str) -> IntentResult | None:
    """Return an IntentResult if a rule matches, else None (ambiguous → defer to LLM)."""
    return _rule_based_cached(_normalize(text))


@lru_cache(maxsize=512)
def _rule_based_cached(norm: str) -> IntentResult | None:
    """Cached implementation — keyed on the normalized string.

    Emojis survive `.strip().lower()`, so the emoji-only check works here too.
    Cache gives ~0ms on repeat inputs (common when users retry the same message).
    """
    if not norm:
        return IntentResult("greeting", 1.0, "rule")

    # "?" / "??" / "huh" mid-flow means the user is confused, NOT greeting.
    # Returning "greeting" here would make the agent reset to a cold-start
    # "Hi I'm Wishkr…" which is the opposite of what they need. Defer to the
    # main agent with an "other" hint so it can repeat its last message.
    if set(norm) == {"?"}:
        return IntentResult("other", 0.9, "rule")

    # Single char or emoji-only — only treat as greeting for letters/emoji.
    # Punctuation-only single chars are caught above.
    if (len(norm) == 1 and norm.isalpha()) or _EMOJI_ONLY_RE.match(norm):
        return IntentResult("greeting", 0.95, "rule")

    # Exact-match short words
    if norm in _MENU_WORDS:
        return IntentResult("menu_request", 1.0, "rule")
    if norm in _AFFIRMATIVE_WORDS:
        return IntentResult("affirmative", 0.95, "rule")
    if norm in _GREETING_WORDS:
        return IntentResult("greeting", 0.95, "rule")
    if norm in _SHOW_WISHLIST_PHRASES:
        return IntentResult("show_wishlist", 0.95, "rule")
    if looks_like_occasion_list_request(norm):
        return IntentResult("show_occasions", 0.95, "rule")

    # Substring rule: any message containing "wishlist" that isn't about
    # adding/removing/clearing is a view request — no LLM needed.
    if "wishlist" in norm:
        _wishlist_mutation_words = {"add", "save", "put", "remove", "delete", "clear", "store"}
        if not (set(norm.split()) & _wishlist_mutation_words):
            return IntentResult("show_wishlist", 0.9, "rule")

    # Very short greetings with punctuation: "hi!", "yo!!"
    stripped = re.sub(r"[!.?,\s]+$", "", norm)
    if stripped in _GREETING_WORDS and len(norm) <= 8:
        return IntentResult("greeting", 0.9, "rule")
    if stripped in _AFFIRMATIVE_WORDS and len(norm) <= 8:
        return IntentResult("affirmative", 0.9, "rule")

    # Product-question detection — require a product keyword AND either an
    # action verb (buy/suggest/want/need/recommend/looking) OR a quality adjective
    # (good/best/cheap/any). Raw product mentions like "I bought a phone for a
    # friend" must NOT fire — those aren't asking Wishkr for the product.
    product_match = _detect_product_question(norm)
    if product_match:
        return product_match

    return None  # ambiguous — ask the LLM


def _looks_like_occasion_list_request(norm: str) -> bool:
    return looks_like_occasion_list_request(norm)


_PRODUCT_ACTION_WORDS = {
    "buy", "buying", "suggest", "suggestion", "suggestions", "recommend",
    "recommendation", "want", "need", "looking", "searching", "find", "get",
    "show", "browse", "pls", "please", "any",
}
_PRODUCT_QUALITY_WORDS = {
    "good", "best", "quality", "cheap", "affordable", "premium", "top",
}
# Phrases that *negate* a product question even if a keyword appears
# (user is describing a past action or a friend's preference, not asking us).
_PRODUCT_NEGATION_PATTERNS = [
    # Past-tense possession → user is describing what's already done
    re.compile(r"\b(bought|got|gave|ordered|purchased)\b"),
    # Someone else's preferences → "friend likes books", "who likes shoes", "she loves X"
    re.compile(
        r"\b(friend|she|he|they|mom|dad|didi|bhai|bro|sis|someone|who)\b"
        r"\s+\w*\s*(likes?|loves?|prefers?|into|enjoys?)\b"
    ),
    # Preference verb directly before product → "likes books", "loves chocolate"
    re.compile(r"\b(likes?|loves?|prefers?|enjoys?)\s+\w*\s*\b"),
]


def _contains_product_keyword(norm: str) -> bool:
    return contains_product_keyword(norm)


def _detect_product_question(norm: str) -> IntentResult | None:
    if len(norm.split()) > 20:
        return None  # very long messages rarely are pure product asks
    if not _contains_product_keyword(norm):
        return None
    # Reject if the sentence is about a past action or someone else's taste
    for pat in _PRODUCT_NEGATION_PATTERNS:
        if pat.search(norm):
            return None
    tokens = set(re.findall(r"[a-z]+", norm))
    has_action = bool(tokens & _PRODUCT_ACTION_WORDS)
    has_quality = bool(tokens & _PRODUCT_QUALITY_WORDS)
    # Very short messages ("earbuds", "ear buds pls") count as asks.
    if len(tokens) <= 4 or has_action or has_quality:
        return IntentResult("product_question", 0.85, "rule")
    return None


# --- LLM fallback -----------------------------------------------------------

_CLASSIFIER_PROMPT = """Classify this WhatsApp user message into ONE intent:

- greeting: hi, hello, hey, yo, namaste, start
- menu_request: asking for options / "menu" / "help"
- affirmative: a plain yes-signal ("yes", "haan", "ok", "sure") with nothing else
- product_question: asking about a specific PHYSICAL product (earbuds, phone, book, clothes, flowers, etc.) — NOT a gift card, NOT a brand like Amazon/Flipkart
- show_wishlist: user wants to see their saved wishlist ("show my wishlist", "what's on my wishlist", "view wishlist")
- other: anything else (adding an occasion, asking about their account, gift card browsing, a question, etc.)

Reply with ONLY the intent name. No punctuation, no explanation.

Message: {msg}"""


_client: "genai.Client | None" = None

_VALID_INTENTS = frozenset({
    "greeting", "menu_request", "affirmative",
    "product_question", "show_wishlist", "other",
})

_CLASSIFIER_TIMEOUT_SECONDS = 3.0


def _get_client() -> "genai.Client":
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _parse_intent(raw: str) -> IntentResult | None:
    cleaned = re.sub(r"[^a-z_]", "", (raw or "").strip().lower())
    if cleaned in _VALID_INTENTS:
        return IntentResult(cleaned, 0.75, "llm")  # type: ignore[arg-type]
    return None


async def _gemini_classify(text: str) -> IntentResult:
    import time
    start_time = time.time()
    client = _get_client()
    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.gemini_fast_model,
                contents=_CLASSIFIER_PROMPT.format(msg=text[:500]),
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=8,
                ),
            ),
            timeout=_CLASSIFIER_TIMEOUT_SECONDS,
        )
        result = _parse_intent(resp.text or "")
        if result:
            return result
        logger.warning("gemini classifier returned unexpected label: %r", resp.text)
        return IntentResult("other", 0.3, "llm")
    finally:
        # Log classifier usage asynchronously (fire and forget)
        latency_ms = (time.time() - start_time) * 1000
        task = asyncio.create_task(_async_log_classifier_usage(
            model=settings.gemini_fast_model,
            provider="google_genai",
            latency_ms=latency_ms,
        ))
        # Add callback to log any unhandled exceptions in the background task
        def _log_task_error(t):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning("classifier usage logging task failed: %s", exc)
        task.add_done_callback(_log_task_error)


async def _deepseek_classify(text: str) -> IntentResult:
    import httpx
    import time
    start_time = time.time()
    async with httpx.AsyncClient(timeout=_CLASSIFIER_TIMEOUT_SECONDS) as client:
        try:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": settings.deepseek_classifier_model,
                    "messages": [
                        {"role": "user", "content": _CLASSIFIER_PROMPT.format(msg=text[:500])},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 8,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            result = _parse_intent(raw)
            if result:
                return result
            logger.warning("deepseek classifier returned unexpected label: %r", raw)
            return IntentResult("other", 0.3, "llm")
        finally:
            latency_ms = (time.time() - start_time) * 1000
            task = asyncio.create_task(_async_log_classifier_usage(
                model=settings.deepseek_classifier_model,
                provider="deepseek",
                latency_ms=latency_ms,
                input_tokens=150,  # rough estimate
                output_tokens=2,
            ))
            # Add callback to log any unhandled exceptions in the background task
            def _log_task_error(t):
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:
                    logger.warning("deepseek classifier usage logging task failed: %s", exc)
            task.add_done_callback(_log_task_error)


async def _llm_classify(text: str) -> IntentResult:
    # Primary: Gemini
    try:
        return await _gemini_classify(text)
    except asyncio.TimeoutError:
        logger.warning("gemini classifier timed out — trying deepseek fallback")
    except Exception as e:
        logger.warning("gemini classifier failed (%s) — trying deepseek fallback", e)

    # Fallback: DeepSeek (only if key is configured)
    if settings.deepseek_api_key:
        try:
            result = await _deepseek_classify(text)
            logger.info("deepseek fallback classifier succeeded: intent=%s", result.intent)
            return result
        except Exception as e:
            logger.warning("deepseek fallback classifier also failed: %s", e)

    return IntentResult("other", 0.0, "llm")


async def classify(text: str) -> IntentResult:
    """Classify a user message. Tries rules first, falls back to LLM."""
    rule = _rule_based(text)
    if rule is not None:
        return rule
    return await _llm_classify(text)


def is_rule_based_affirmative(text: str) -> bool:
    """True only for rock-solid yes-signals (used by deterministic router).

    Deliberately rule-only — never invokes the LLM — so callers can branch
    synchronously on it without latency or cost.
    """
    rule = _rule_based(text)
    return bool(rule and rule.intent == "affirmative")
