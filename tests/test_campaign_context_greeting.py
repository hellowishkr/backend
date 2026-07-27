"""Message-match: campaign-aware greeting for inbound "hi" replies.

Covers ``_load_and_consume_campaign_context`` (app/bot/agent.py) and
``acquisition_greeting_for`` (app/bot/whatsapp_templates.py), the two
deterministic pieces the safe-greeting short-circuit uses to pick
campaign-specific copy for a brand-new phone's first reply.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

import google.adk.sessions as _adk_sessions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import agent  # noqa: E402
from app.bot.whatsapp_templates import acquisition_greeting_for, ACQUISITION_GREETING_COPY  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import CampaignContext, User  # noqa: E402

T = TypeVar("T")


def _run_db(fn: Callable[[object], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [User.__table__, CampaignContext.__table__]
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))

        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


PHONE = "919888888888"


# ─── acquisition_greeting_for: pure copy resolution ──────────────────────

def test_known_variant_returns_its_specific_copy():
    reply = acquisition_greeting_for("welcome_problem_duplicate")
    assert "avoid buying the same gift" in reply
    assert reply.endswith("what should I call you?")


def test_unknown_or_default_variant_returns_generic_copy():
    default = ACQUISITION_GREETING_COPY["_default"]
    assert acquisition_greeting_for("welcome_allinone") == default
    assert acquisition_greeting_for("some_future_template_not_yet_mapped") == default
    assert acquisition_greeting_for(None) == default


# ─── _load_and_consume_campaign_context: DB read + consume + TTL ─────────

def test_fresh_unconsumed_context_is_returned_and_marked_consumed():
    async def scenario(db):
        db.add(CampaignContext(
            phone=PHONE,
            template_name="welcome_problem_reminder",
            sent_at=datetime.now(timezone.utc),
        ))
        await db.flush()

        template = await agent._load_and_consume_campaign_context(db, PHONE)
        await db.flush()

        row = (await db.execute(
            select(CampaignContext).where(CampaignContext.phone == PHONE)
        )).scalar_one()
        return template, row.consumed_at

    template, consumed_at = _run_db(scenario)
    assert template == "welcome_problem_reminder"
    assert consumed_at is not None


def test_already_consumed_context_is_not_replayed():
    async def scenario(db):
        db.add(CampaignContext(
            phone=PHONE,
            template_name="welcome_problem_gifts",
            sent_at=datetime.now(timezone.utc),
            consumed_at=datetime.now(timezone.utc),
        ))
        await db.flush()
        return await agent._load_and_consume_campaign_context(db, PHONE)

    assert _run_db(scenario) is None


def test_stale_context_past_max_age_is_ignored():
    async def scenario(db):
        stale_sent_at = datetime.now(timezone.utc) - timedelta(days=8)
        db.add(CampaignContext(
            phone=PHONE,
            template_name="welcome_problem_duplicate",
            sent_at=stale_sent_at,
        ))
        await db.flush()
        return await agent._load_and_consume_campaign_context(db, PHONE)

    assert _run_db(scenario) is None


def test_no_context_row_returns_none():
    async def scenario(db):
        return await agent._load_and_consume_campaign_context(db, "919000000000")

    assert _run_db(scenario) is None
