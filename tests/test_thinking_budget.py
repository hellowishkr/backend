"""The main agent must cap Gemini's internal reasoning budget.

Production (30 Jul 2026, 05:47): a single turn spent 1399 thinking tokens to
produce a 27-token reply, 7.6s server-side (Server-Timing dur=7643) — with the
prompt already reduced to 7983 tokens and the second LLM call eliminated.
Thinking, not prompt size, was the dominant latency term.

Left unconfigured, 2.5-flash picks its own budget per request, so latency has no
ceiling. These tests guard that a budget is set and stays sane.
"""
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.core.config import settings  # noqa: E402


def test_thinking_budget_is_configured():
    assert settings.gemini_thinking_budget is not None


def test_thinking_budget_leaves_room_for_tool_choice():
    """0 would disable thinking entirely.

    The agent picks among up to 25 tools and must reason about which to call;
    a hard 0 risks worse tool selection, which costs more than it saves. -1
    (dynamic) is what caused the 7.6s tail, so that is not acceptable either.
    """
    budget = settings.gemini_thinking_budget
    assert budget > 0, "0 disables thinking and risks degrading tool choice"
    assert budget != -1, "-1 restores the unbounded dynamic default"


def test_thinking_budget_below_observed_overspend():
    """Must be tighter than the 1399 tokens observed in production."""
    assert settings.gemini_thinking_budget < 1399


def test_gen_config_passes_thinking_budget_to_gemini():
    """A setting nothing reads would be a silent no-op."""
    import inspect

    from app.bot import agent

    src = inspect.getsource(agent._build_runner)
    assert "thinking_config" in src, "thinking_config missing from generate config"
    assert "gemini_thinking_budget" in src, "budget must come from settings, not a literal"
