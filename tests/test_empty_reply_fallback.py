"""Empty-reply fallback rotation: consecutive picks must never repeat.

The outbound dedup (app/webhooks/whatsapp.py) drops identical (phone, body)
pairs within 60s; a repeated fallback body means the user gets silence.
"""
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        self.kwargs = _kw
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.bot import agent  # noqa: E402


def test_empty_fallback_counter_rotates():
    # agent.py picks reply = variants[next(counter) % 3]; a strictly
    # incrementing counter guarantees consecutive picks differ.
    first = next(agent._EMPTY_FALLBACK_COUNTER)
    second = next(agent._EMPTY_FALLBACK_COUNTER)
    assert second == first + 1
