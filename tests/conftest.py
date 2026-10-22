"""Pytest fixtures for async database tests."""

import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.base import Base
from app.models import (
    User,
    Contact,
    WishlistItem,
    LlmUsageEvent,
    Occasion,
    RecipientPreference,
    ConversationMessage,
    ConversationState,
    GiftLog,
    CampaignContext,
    Reminder,
    OccasionGiftPlan,
    Product,
    Vendor,
    CelebrantOccasion,
    CelebrantFriend,
    Purchase,
    Order,
)
from app.models.recipient_preference import PreferenceRequest


# Only create tables used in tests
_TABLES = [
    User.__table__,
    Contact.__table__,
    WishlistItem.__table__,
    LlmUsageEvent.__table__,
    Occasion.__table__,
    RecipientPreference.__table__,
    ConversationMessage.__table__,
    ConversationState.__table__,
    PreferenceRequest.__table__,
    GiftLog.__table__,
    CampaignContext.__table__,
    Reminder.__table__,
    OccasionGiftPlan.__table__,
    Vendor.__table__,
    Product.__table__,
    CelebrantOccasion.__table__,
    CelebrantFriend.__table__,
    # Occasion.purchases is a lazy relationship, so deleting an occasion makes
    # SQLAlchemy read this table even when no purchase exists.
    Purchase.__table__,
    Order.__table__,
]


@pytest.fixture(autouse=True)
def _no_live_intent_router(monkeypatch):
    """Keep the intent router off the network in tests.

    The router calls DeepSeek on every turn, so without this the suite makes
    real HTTP calls: slow, flaky, costly, and dependent on someone else's
    uptime. Tests that care about routing patch `route` themselves; everything
    else gets the degraded result, which is exactly what production falls back
    to when DeepSeek is unavailable.
    """
    import sys

    from app.bot import intent_router

    async def _degraded(_db, _phone, _text):
        return intent_router.DEGRADED

    monkeypatch.setattr(intent_router, "route", _degraded)

    # Belt and braces: `_call_model` is the only place that touches the
    # network, so stubbing it means even a module that captured `route` before
    # this fixture ran (or one imported later) cannot reach DeepSeek.
    async def _no_network(*_a, **_kw):
        raise AssertionError(
            "intent_router tried to call DeepSeek during a test — "
            "patch `route` in the test if routing behaviour is under test"
        )

    monkeypatch.setattr(intent_router, "_call_model", _no_network)

    # Patch the agent's re-exported alias only if the module is ALREADY
    # imported. Importing app.bot.agent here would build its ADK session
    # service, which needs a live Postgres — tests that never touch the agent
    # must not require one.
    agent_module = sys.modules.get("app.bot.agent")
    if agent_module is not None:
        monkeypatch.setattr(agent_module, "route_intent", _degraded, raising=False)


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db() -> AsyncSession:
    """Provide an async database session with in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with Session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def model_turn(monkeypatch):
    """Exercise real orchestration/tools while replacing only model/network edges."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.bot import agent
    from app.bot.intent_router import DEGRADED
    from app.bot.tools.adapter import execute_tool
    from app.utils.circuit_breaker import CircuitBreaker

    @asynccontextmanager
    async def no_db_lock(*args):
        yield

    monkeypatch.setattr(agent, "_db_phone_lock", no_db_lock)
    monkeypatch.setattr(
        agent,
        "_get_or_create_session",
        AsyncMock(return_value=(SimpleNamespace(id="test_session", events=[]), False)),
    )
    monkeypatch.setattr(agent, "_trim_session_events", AsyncMock())
    monkeypatch.setattr(agent, "_log_llm_usage", AsyncMock())
    monkeypatch.setattr(agent, "get_daily_budget_usage", AsyncMock(return_value=(0, 100, 0)))
    monkeypatch.setattr(agent, "check_all_budgets", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(
        agent, "check_and_bump", AsyncMock(return_value=SimpleNamespace(allowed=True))
    )
    monkeypatch.setattr(
        agent,
        "_gemini_breaker",
        CircuitBreaker(failure_threshold=5, window_seconds=30, cooldown_seconds=30),
    )

    def setup(reply, actions=(), route=DEGRADED):
        seen = {"messages": [], "results": []}

        async def run_async(**kwargs):
            seen["messages"].append(kwargs["new_message"].parts[0].text)
            for name, args in actions:
                yield SimpleNamespace(
                    content=None, get_function_calls=lambda: [SimpleNamespace(name=name, args=args)]
                )
                result = await execute_tool(name, args)
                seen["results"].append(result)
                yield SimpleNamespace(
                    content=None,
                    get_function_responses=lambda: [SimpleNamespace(name=name, response=result)],
                )
            yield SimpleNamespace(
                content=SimpleNamespace(role="model", parts=[SimpleNamespace(text=reply)])
            )

        runner = SimpleNamespace(run_async=run_async)
        get_runner = lambda *args: runner
        monkeypatch.setattr(agent, "_get_runner_for_phase", get_runner)
        monkeypatch.setattr(agent, "route_intent", AsyncMock(return_value=route))
        return seen

    return setup
