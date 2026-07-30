"""Gift asks must route to the discovery subflow, not the everything-bucket.

Production (30 Jul 2026): "gift for my sister under 2000" classified as `other`
and fell through to `default`, shipping all 24 tool declarations (~18.3k tokens)
on the most common request shape Wishkr exists to serve. There was no intent
label for "wants gift ideas, named no product" — product_question explicitly
excludes it — so the classifier call ran and changed nothing.
"""
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.bot import agent  # noqa: E402
from app.bot.intent_classifier import (  # noqa: E402
    _CLASSIFIER_PROMPT,
    _VALID_INTENTS,
    IntentResult,
)


def _select(msg: str, intent: str):
    return agent._select_tools_for_turn(
        "browse", msg, IntentResult(intent=intent, confidence=0.9, source="llm")
    )


def test_gift_suggestion_narrows_tools():
    full = len(agent._PHASE_TOOLS["browse"])
    selected, subflow = _select("gift for my sister under 2000", "gift_suggestion")
    assert subflow == "product_discovery"
    assert len(selected) < full, "gift asks must not ship the full tool surface"


def test_gift_suggestion_keeps_add_occasion():
    """"birthday gift for mom" is a gift ask AND an occasion worth saving.

    Reminders are the core product; dropping add_occasion here would force the
    user to repeat the date in a second turn.
    """
    selected, _ = _select("birthday gift for mom", "gift_suggestion")
    assert "add_occasion" in {t.name for t in selected}


def test_suggest_gifts_present_for_gift_intent():
    selected, _ = _select("what should I get my wife", "gift_suggestion")
    assert "suggest_gifts" in {t.name for t in selected}


def test_keyword_backstop_still_works_without_classifier():
    """When the classifier is down every message degrades to `other`.

    The keyword path must still catch explicit phrasing, otherwise a classifier
    outage silently doubles token cost on every gift ask.
    """
    selected, subflow = _select("suggest a gift for my sister", "other")
    assert subflow == "product_discovery"
    assert len(selected) < len(agent._PHASE_TOOLS["browse"])


def test_bare_gift_ask_without_classifier_falls_back_to_default():
    """Documents a known limitation rather than asserting ideal behaviour.

    "gift for my sister" has no discovery keyword, so with the classifier
    unavailable it still routes to `default`. Acceptable: correctness over
    token savings when we cannot tell a gift ask from an occasion edit.
    """
    _, subflow = _select("gift for my sister under 2000", "other")
    assert subflow == "default"


def test_classifier_labels_are_consistent():
    """Every label the prompt offers must be accepted, and vice versa.

    show_occasions was in the Intent type and returned by the rule path, but
    missing from both the prompt and _VALID_INTENTS — so the LLM could never
    emit it and would have had it discarded if it did.
    """
    offered = {
        line.split(":")[0].strip("- ").strip()
        for line in _CLASSIFIER_PROMPT.splitlines()
        if line.startswith("- ")
    }
    assert offered == set(_VALID_INTENTS), (
        f"prompt offers {offered - set(_VALID_INTENTS)}, "
        f"valid set has extra {set(_VALID_INTENTS) - offered}"
    )


def test_gift_suggestion_is_a_valid_intent():
    assert "gift_suggestion" in _VALID_INTENTS
