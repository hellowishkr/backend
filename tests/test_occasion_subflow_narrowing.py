"""Occasion-keyword turns must get the narrowed tool set, not all 24 tools.

Large tool counts correlate with Gemini 2.5 Flash's empty-candidate flake
(2026-07-23 prod incident) — occasion/reminder turns are the top query
category and should ride the smaller declaration payload.
"""
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        self.kwargs = _kw
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.bot.agent import _PHASE_TOOLS, _select_tools_for_turn  # noqa: E402


_OTHER = SimpleNamespace(intent="other")


def test_upcoming_occasions_gets_occasion_subflow():
    tools, subflow = _select_tools_for_turn(
        "browse", "What are my upcoming ocassions?", _OTHER,
    )
    assert subflow == "occasions"
    assert len(tools) < len(_PHASE_TOOLS["browse"])
    names = {t.name for t in tools}
    assert "get_my_occasions" in names
    assert "suggest_gifts" in names  # gift asks co-occur with occasion talk


def test_birthday_reminder_gets_occasion_subflow():
    _, subflow = _select_tools_for_turn(
        "browse", "remind me about riya birthday", _OTHER,
    )
    assert subflow == "occasions"


def test_discovery_signal_outranks_occasion_signal():
    _, subflow = _select_tools_for_turn(
        "browse", "suggest a gift for her birthday", _OTHER,
    )
    assert subflow == "product_discovery"


def test_plain_message_keeps_full_toolset():
    tools, subflow = _select_tools_for_turn("browse", "hello", _OTHER)
    assert subflow == "default"
    assert len(tools) == len(_PHASE_TOOLS["browse"])
