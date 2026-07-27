from __future__ import annotations

import time
from types import SimpleNamespace

import google.adk.sessions as _adk_sessions
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError, ProgrammingError


class _StubSessionService:
    def __init__(self, **_kw):
        self.kwargs = _kw
        self.db_engine = SimpleNamespace(dispose=lambda: None)


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import agent  # noqa: E402
from app.db.session import create_resilient_async_engine  # noqa: E402


def _make_session(n: int = 1, *, app_name: str = "wishkr", user_id: str = "91x"):
    return SimpleNamespace(
        app_name=app_name,
        user_id=user_id,
        id=f"sess-{n}",
        events=[],
        last_update_time=time.time(),
    )


class _FlakySessionService:
    """Stub that optionally raises a transient OperationalError on the first list_sessions call."""

    def __init__(self, *, fail_first_list: bool = False, fail_exc: Exception | None = None):
        self.fail_first_list = fail_first_list
        self.fail_exc = fail_exc or OperationalError(
            statement="SELECT 1",
            params={},
            orig=Exception("server closed the connection unexpectedly"),
        )
        self.list_calls = 0
        self.create_calls = 0
        self.delete_calls = 0
        self.db_engine = SimpleNamespace(dispose=lambda: None)

    async def list_sessions(self, *, app_name: str, user_id: str):
        self.list_calls += 1
        if self.fail_first_list:
            self.fail_first_list = False
            raise self.fail_exc
        return SimpleNamespace(sessions=[])

    async def create_session(self, *, app_name: str, user_id: str):
        self.create_calls += 1
        return _make_session(self.create_calls, app_name=app_name, user_id=user_id)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str):
        self.delete_calls += 1
        return None

    async def get_session(self, **_kwargs):
        return None


class _AlwaysFailsService:
    """Stub that always raises the given exception on list_sessions."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.list_calls = 0
        self.create_calls = 0
        self.db_engine = SimpleNamespace(dispose=lambda: None)

    async def list_sessions(self, **_kw):
        self.list_calls += 1
        raise self.exc

    async def create_session(self, *, app_name: str, user_id: str):
        self.create_calls += 1
        return _make_session(self.create_calls)

    async def delete_session(self, **_kw):
        return None

    async def get_session(self, **_kw):
        return None


def test_adk_session_engine_kwargs_include_pool_settings():
    """ADK sync engine must have pool_pre_ping, pool_recycle, pool_size, max_overflow."""
    kwargs = agent._ADK_SESSION_ENGINE_KWARGS
    assert kwargs.get("pool_pre_ping") is True
    assert kwargs.get("pool_recycle") == 300
    assert "pool_size" in kwargs
    assert "max_overflow" in kwargs


@pytest.mark.asyncio
async def test_resilient_async_engine_enables_pre_ping_and_recycle():
    engine = create_resilient_async_engine("postgresql+asyncpg://wishkr:test@localhost:5432/wishkr")
    try:
        assert engine.sync_engine.pool._pre_ping is True  # type: ignore[attr-defined]
        assert engine.sync_engine.pool._recycle == 300  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_or_create_session_retries_after_transient_db_disconnect(monkeypatch):
    """Transient OperationalError on list_sessions triggers rebuild and one retry."""
    first = _FlakySessionService(fail_first_list=True)
    second = _FlakySessionService()

    def fake_build_session_service():
        return second

    monkeypatch.setattr(agent, "_session_service", first, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", fake_build_session_service)
    agent._runners.clear()
    agent._runners["stale"] = object()

    session, is_new = await agent._get_or_create_session("919999999999")

    assert is_new is True
    assert session.id == "sess-1"
    assert first.list_calls == 1
    assert first.create_calls == 0
    assert second.list_calls == 1
    assert second.create_calls == 1
    assert second.delete_calls == 0
    assert agent._runners == {}


@pytest.mark.asyncio
async def test_get_or_create_session_retries_after_transient_interface_error(monkeypatch):
    """Transient InterfaceError (connection already closed) also triggers rebuild and retry."""
    interface_exc = InterfaceError(
        statement="SELECT 1",
        params={},
        orig=Exception("connection is closed"),
    )
    first = _FlakySessionService(fail_first_list=True, fail_exc=interface_exc)
    second = _FlakySessionService()

    monkeypatch.setattr(agent, "_session_service", first, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", lambda: second)
    agent._runners.clear()

    session, is_new = await agent._get_or_create_session("919999999999")

    assert is_new is True
    assert first.list_calls == 1
    assert second.list_calls == 1
    assert second.create_calls == 1


@pytest.mark.asyncio
async def test_session_service_call_does_not_retry_programming_error(monkeypatch):
    """ProgrammingError (schema bug etc.) must NOT be retried — it is not transient."""
    prog_exc = ProgrammingError(
        statement="SELECT 1",
        params={},
        orig=Exception("relation does not exist"),
    )
    bad_service = _AlwaysFailsService(prog_exc)
    good_service = _FlakySessionService()  # would succeed if incorrectly reached

    monkeypatch.setattr(agent, "_session_service", bad_service, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", lambda: good_service)
    agent._runners.clear()

    with pytest.raises(ProgrammingError):
        await agent._session_service_call("list_sessions", app_name="wishkr", user_id="91x")

    assert bad_service.list_calls == 1
    assert good_service.list_calls == 0  # rebuild was never triggered


@pytest.mark.asyncio
async def test_session_service_call_retries_only_once(monkeypatch):
    """If both attempts fail with a transient error, the second is re-raised without looping."""
    transient_exc = OperationalError(
        statement="SELECT 1",
        params={},
        orig=Exception("server closed the connection unexpectedly"),
    )
    first = _AlwaysFailsService(transient_exc)
    second = _AlwaysFailsService(transient_exc)

    call_count = {"n": 0}

    def fake_build():
        call_count["n"] += 1
        return second

    monkeypatch.setattr(agent, "_session_service", first, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", fake_build)
    agent._runners.clear()

    with pytest.raises(OperationalError):
        await agent._session_service_call("list_sessions", app_name="wishkr", user_id="91x")

    # Rebuilt exactly once; second attempt hit the re-raised error
    assert call_count["n"] == 1
    assert first.list_calls == 1
    assert second.list_calls == 1


@pytest.mark.asyncio
async def test_session_service_rebuilt_and_old_engine_disposed_on_transient_error(monkeypatch):
    """Engine.dispose() is called on the old service when a transient error triggers rebuild."""
    disposed = {"called": False}

    old_engine = SimpleNamespace(dispose=lambda: disposed.__setitem__("called", True))
    first = _FlakySessionService(fail_first_list=True)
    first.db_engine = old_engine
    second = _FlakySessionService()

    monkeypatch.setattr(agent, "_session_service", first, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", lambda: second)
    agent._runners.clear()

    await agent._get_or_create_session("919999999999")

    assert disposed["called"] is True, "old db_engine.dispose() must be called on rebuild"
