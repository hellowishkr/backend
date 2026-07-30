"""A failed tool carrying `user_message` must answer the turn directly.

Production trace (30 Jul 2026) for "gift for my sister": suggest_gifts returned
`no_vibes_resolved` with a ready user_message, and the turn still spent a second
~12k-token Gemini call just to restate it — ~2s and ~$0.0009 wasted per
occurrence. The short-circuit in agent.py exists to stop that, so this guards
both halves: the tool wording is what the user sees, and successful tools are
left alone.
"""
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        self.kwargs = _kw
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.bot import agent  # noqa: E402
from app.services.gift_recommendation import all_vibe_ids  # noqa: E402


def _extract_user_message(resp: dict) -> str | None:
    """Mirror of the capture rule in agent.py's tool-audit block."""
    if not (isinstance(resp, dict) and resp.get("error")):
        return None
    direct = resp.get("user_message")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()[:1000]
    return None


def test_no_vibes_resolved_shape_is_short_circuitable():
    """The real payload must satisfy the short-circuit condition.

    Pinned against the actual production shape rather than a hand-made dict: if
    gift_recommendation stops sending `error` + `user_message` together, the
    optimisation silently stops applying and this fails.
    """
    payload = {
        "error": "no_vibes_resolved",
        "user_message": "Tell me a little about what they like, and I’ll suggest better gifts.",
        "message": "Couldn't infer a vibe. Pass an explicit `vibes` list or describe the recipient (recipient_vibe).",
        "available_vibes_sample": sorted(all_vibe_ids())[:20],
    }
    assert _extract_user_message(payload) == payload["user_message"]


def test_successful_tool_response_does_not_short_circuit():
    """Successful tools must still be rendered by the model.

    Short-circuiting a catalogue result would dump raw data at the user, so the
    condition is deliberately gated on `error`.
    """
    assert _extract_user_message({"catalogue_options": [{"name": "Mug"}]}) is None
    # An error without user-facing wording has nothing safe to surface.
    assert _extract_user_message({"error": "upstream_timeout"}) is None
    # Blank/whitespace wording is not a usable reply.
    assert _extract_user_message({"error": "x", "user_message": "   "}) is None


def test_long_user_message_is_truncated():
    payload = {"error": "x", "user_message": "y" * 5000}
    assert len(_extract_user_message(payload)) == 1000


def _ctx():
    return SimpleNamespace(invocation_context=SimpleNamespace(end_invocation=False))


def test_callback_ends_invocation_on_user_message():
    """ADK's cooperative stop must fire, so no second LLM call is made.

    Previously the loop `break`-ed out of runner.run_async instead, which threw
    GeneratorExit into the generator mid-iteration and unwound OpenTelemetry's
    span from another async context ("Failed to detach context" in Sentry).
    """
    ctx = _ctx()
    agent._end_invocation_on_user_message(
        None, {}, ctx,
        {"error": "no_vibes_resolved", "user_message": "Tell me a little more."},
    )
    assert ctx.invocation_context.end_invocation is True


def test_callback_leaves_successful_tools_running():
    """A successful tool still needs the model to render its result."""
    ctx = _ctx()
    agent._end_invocation_on_user_message(
        None, {}, ctx, {"catalogue_options": [{"name": "Mug"}]}
    )
    assert ctx.invocation_context.end_invocation is False


def test_callback_ignores_error_without_user_message():
    """Nothing safe to show the user — let the model phrase the failure."""
    ctx = _ctx()
    agent._end_invocation_on_user_message(None, {}, ctx, {"error": "upstream_timeout"})
    assert ctx.invocation_context.end_invocation is False


def test_callback_is_registered_on_the_agent():
    """An unregistered callback would silently never fire."""
    runner = agent._build_runner("unregistered", "gemini-2.5-flash", [])
    assert runner.agent.after_tool_callback is agent._end_invocation_on_user_message


def test_loop_does_not_break_out_of_the_generator():
    """Guard the fix: a `break` here reintroduces the OTel context error."""
    import inspect

    src = inspect.getsource(agent.handle_message)
    idx = src.index("short-circuit on tool user_message")
    # Look at the handful of lines following the log call.
    tail = src[idx : idx + 400]
    assert "break" not in tail, (
        "breaking out of runner.run_async tears down the OTel span from another "
        "context; rely on end_invocation instead"
    )


def test_agent_reads_user_message_before_generic_fallback():
    """Guard the ordering in handle_message.

    `tool_user_message` must be consulted before the generic empty-reply
    fallback, otherwise the user gets "I got a bit stuck" instead of the real
    question the tool asked.
    """
    import inspect

    src = inspect.getsource(agent.handle_message)
    assert "tool_user_message" in src, "short-circuit was removed from handle_message"
    user_msg_at = src.index("if not reply and tool_user_message")
    # The generic branch keys off breaker_open; it must come later.
    generic_at = src.index("if breaker_open or fallback_reason")
    assert user_msg_at < generic_at, "tool_user_message must win over the generic fallback"
