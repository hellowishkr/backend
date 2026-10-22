"""Minimal deterministic signals kept after the move to LLM intent routing.

Intent *understanding* now lives in `app/bot/intent_router.py`, which reads the
conversation with history. What survives here is the small part that must stay
synchronous and rule-based:

  * ``IntentResult`` — the routing-hint shape several modules type against.
    ``RouteResult`` in `intent_router` is a superset of it.
  * ``is_rule_based_affirmative`` — used by `webhooks/whatsapp.py:get_ack()`,
    which is documented "<5ms, no LLM" and runs before the reply is generated.
    It cannot await a network call.

The removed half (``classify``, ``_rule_based``, ``_gemini_classify``,
``_llm_classify``, the phrase tables and the product-question detector) decided
intent from the bare message string with no history. That is exactly why a
reply to the bot's own question — "Myra - birthday - 7th Aug" — was labelled
`show_occasions` and answered with "I don't have any occasions saved for you
yet" while occasions existed, losing six occasions across eleven turns in the
2026-08 transcript. Do not reintroduce keyword intent detection here; add to
the router's prompt instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.bot.language_map import AFFIRMATIVE_WORDS

Intent = Literal[
    "greeting", "menu_request", "affirmative", "product_question",
    "gift_suggestion", "show_wishlist", "show_occasions", "other",
]


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    confidence: float  # 0.0–1.0
    source: Literal["rule", "llm"]


_TRAILING_PUNCT_RE = re.compile(r"[!.?,\s]+$")


def is_rule_based_affirmative(text: str) -> bool:
    """True only for rock-solid yes-signals ("yes", "haan", "ok", "👍").

    Deliberately rule-only and synchronous — `get_ack()` branches on this to
    produce an instant acknowledgement, so it must never invoke an LLM or
    otherwise block. Matches the WHOLE message only: "yes please, but for my
    sister instead" is not a bare affirmative and must reach the agent.
    """
    norm = " ".join((text or "").strip().lower().split())
    if not norm or len(norm) > 12:
        return False
    if re.fullmatch(r"ye+s+[!.?]*", norm):
        return True
    return _TRAILING_PUNCT_RE.sub("", norm) in AFFIRMATIVE_WORDS
