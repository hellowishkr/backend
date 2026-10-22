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
from app.bot.intent_router import DEGRADED, RouteResult  # noqa: E402


def _select_routed(msg: str, *tools: str):
    """Tool selection when the intent router named `tools`."""
    return agent._select_tools_for_turn(
        "browse", msg,
        RouteResult(
            intent="suggest_gift", confidence=0.9, source="llm",
            likely_tools=tools,
        ),
    )


def _select_degraded(msg: str):
    """Tool selection when the router was unavailable (keyword fallback)."""
    return agent._select_tools_for_turn("browse", msg, DEGRADED)


def test_routed_gift_ask_narrows_tools():
    full = len(agent._PHASE_TOOLS["browse"])
    selected, subflow = _select_routed(
        "gift for my sister under 2000", "suggest_gifts",
    )
    assert subflow == "routed"
    assert len(selected) < full, "gift asks must not ship the full tool surface"
    assert "suggest_gifts" in {t.name for t in selected}


def test_routed_friend_gift_ask_keeps_context_lookup_available():
    selected, subflow = agent._select_tools_for_turn(
        "friend",
        "help me continue this gift plan",
        RouteResult(
            intent="suggest_gift",
            confidence=0.9,
            source="llm",
            likely_tools=("suggest_gifts",),
        ),
    )

    assert subflow == "routed"
    assert {"suggest_gifts", "get_celebrant_context"} <= {
        tool.name for tool in selected
    }


def test_routed_occasion_save_narrows_hard():
    """The most common turn in the product must be the cheapest.

    An occasion save needs add_occasion (plus edit_occasion for an immediate
    correction) — not the full 24-declaration surface.
    """
    full = len(agent._PHASE_TOOLS["browse"])
    selected, subflow = _select_routed("Myra - birthday - 7th Aug", "add_occasion")
    names = {t.name for t in selected}
    assert subflow == "routed"
    assert "add_occasion" in names
    assert len(selected) < full // 2, f"expected a tight set, got {sorted(names)}"


def test_router_tool_names_come_from_the_registry():
    """A tool the registry does not know must never reach Gemini."""
    from app.bot.tools.schemas import TOOL_SPECS

    selected, _ = _select_routed("anything", "suggest_gifts")
    assert {t.name for t in selected} <= set(TOOL_SPECS)


def test_keyword_backstop_still_works_when_router_is_down():
    """A DeepSeek outage must not silently double token cost on gift asks."""
    selected, subflow = _select_degraded("suggest a gift for my sister")
    assert subflow == "default"
    assert len(selected) == len(agent._PHASE_TOOLS["browse"])


def test_friend_keyword_backstop_keeps_context_lookup_available():
    selected, subflow = agent._select_tools_for_turn(
        "friend", "suggest a birthday gift", DEGRADED,
    )

    assert subflow == "default"
    assert "get_celebrant_context" in {tool.name for tool in selected}


def test_bare_gift_ask_when_router_is_down_falls_back_to_default():
    """Documents a known limitation rather than asserting ideal behaviour.

    "gift for my sister" carries no discovery keyword, so with the router
    unavailable it routes to `default` and ships the full surface. Correctness
    over token savings when we cannot tell a gift ask from an occasion edit.
    """
    _, subflow = _select_degraded("gift for my sister under 2000")
    assert subflow == "default"


def test_degraded_result_narrows_nothing_by_itself():
    """Fail-open contract: a degraded route must not restrict Gemini's tools."""
    selected, _ = _select_degraded("something completely unclassifiable zzz")
    assert len(selected) == len(agent._PHASE_TOOLS["browse"])


# --- Every branching label must have a producer -----------------------------


def test_labels_consumed_by_code_are_in_the_router_vocabulary():
    """Labels that code branches on must be ones the router can actually emit.

    Regression guard for a specific failure mode: the router's prompt once
    asked for a free-form "<short_snake_case_label>", so `product_question` and
    `show_wishlist` — which `action_router` branches on — had NO producer. The
    product gate and the wishlist-link gate became unreachable while still
    reading as live code. If a branch is added for a new label, the label must
    join VALID_INTENTS (and the prompt) in the same change.
    """
    from app.bot.intent_router import VALID_INTENTS

    consumed = {
        "product_question",   # action_router product gate
        "show_wishlist",      # action_router stale-awaiting clear
        "affirmative",        # agent._intent_hint
        "greeting",           # agent._intent_hint
    }
    missing = consumed - VALID_INTENTS
    assert not missing, f"code branches on labels the router cannot emit: {missing}"


def test_every_vocabulary_label_is_offered_by_the_prompt():
    """The prompt and the accepted set must not drift apart.

    A label in VALID_INTENTS but absent from the prompt can never be emitted;
    one in the prompt but not the set is silently coerced to "other".
    """
    from app.bot.intent_router import _PROMPT, VALID_INTENTS

    offered = {
        line.split(":")[0].strip("- ").strip()
        for line in _PROMPT.splitlines()
        if line.startswith("- ") and ":" in line
    }
    assert VALID_INTENTS <= offered, f"not offered by prompt: {VALID_INTENTS - offered}"


def test_off_vocabulary_label_is_coerced_not_propagated():
    """A hallucinated label must never reach a downstream branch."""
    import json

    from app.bot.intent_router import _parse

    result = _parse(json.dumps({
        "intent": "some_invented_label",
        "likely_tools": ["add_occasion"],
        "confidence": 0.9,
    }))
    assert result.intent == "other"
    # The useful signal survives coercion.
    assert result.likely_tools == ("add_occasion",)
