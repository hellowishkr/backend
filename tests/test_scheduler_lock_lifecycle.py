from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app import main


class _DummyApp:
    def __init__(self):
        self.state = SimpleNamespace()


@pytest.mark.asyncio
async def test_scheduler_lock_acquisition_commits_autobegun_transaction(monkeypatch):
    """The session lock must not remain idle inside an open transaction."""
    fake_conn = SimpleNamespace(
        scalar=AsyncMock(side_effect=[321, True]),
        commit=AsyncMock(),
        close=AsyncMock(),
    )
    fake_engine = SimpleNamespace(connect=AsyncMock(return_value=fake_conn))

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "engine", fake_engine)

    acquired = await main._try_acquire_scheduler_lock()

    assert acquired is fake_conn
    fake_conn.commit.assert_awaited_once_with()
    fake_conn.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_lock_release_commits_before_closing():
    fake_conn = SimpleNamespace(
        closed=False,
        execute=AsyncMock(),
        commit=AsyncMock(),
        close=AsyncMock(),
    )

    await main._release_scheduler_lock(fake_conn)

    fake_conn.execute.assert_awaited_once()
    fake_conn.commit.assert_awaited_once_with()
    fake_conn.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_lifespan_lock_acquired_starts_and_stops_scheduler(monkeypatch):
    app = _DummyApp()
    fake_conn = SimpleNamespace(execute=AsyncMock(), close=AsyncMock())

    monkeypatch.setattr(main, "_validate_config", lambda: None)
    monkeypatch.setattr(main.settings, "run_scheduler", True)
    monkeypatch.setattr(main, "_try_acquire_scheduler_lock", AsyncMock(return_value=fake_conn))
    monkeypatch.setattr(main, "_release_scheduler_lock", AsyncMock())

    import app.jobs.scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: setattr(app.state, "_started", True))
    monkeypatch.setattr(sched, "stop_scheduler", lambda: setattr(app.state, "_stopped", True))

    import app.webhooks.whatsapp as wa
    monkeypatch.setattr(wa, "drain_active_tasks", AsyncMock())

    async with main.lifespan(app):
        assert getattr(app.state, "scheduler_started", False) is True
        assert getattr(app.state, "_started", False) is True

    assert getattr(app.state, "_stopped", False) is True
    main._release_scheduler_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_lock_denied_does_not_start_or_stop_scheduler(monkeypatch):
    app = _DummyApp()

    monkeypatch.setattr(main, "_validate_config", lambda: None)
    monkeypatch.setattr(main.settings, "run_scheduler", True)
    monkeypatch.setattr(main, "_try_acquire_scheduler_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_release_scheduler_lock", AsyncMock())

    flags = {"started": 0, "stopped": 0}
    import app.jobs.scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: flags.__setitem__("started", flags["started"] + 1))
    monkeypatch.setattr(sched, "stop_scheduler", lambda: flags.__setitem__("stopped", flags["stopped"] + 1))

    import app.webhooks.whatsapp as wa
    monkeypatch.setattr(wa, "drain_active_tasks", AsyncMock())

    async with main.lifespan(app):
        assert getattr(app.state, "scheduler_started", True) is False

    assert flags["started"] == 0
    assert flags["stopped"] == 0
    main._release_scheduler_lock.assert_awaited_once_with(None)
