"""End-to-end message-match: template send records context, then "hi" uses it.

Proves the full loop the campaign tool relies on:
  1. send_template_and_record() writes a CampaignContext row.
  2. handle_message() on a brand-new phone's first "hi" reads it back,
     replies with the campaign-specific greeting, and consumes the row.
  3. The main LLM (_get_runner_for_phase) is never invoked for that turn.

``_get_or_create_session`` is patched directly rather than relying on the
real ADK DatabaseSessionService singleton: app.bot.agent builds that
singleton once at module-import time, and several other test files
monkeypatch google.adk.sessions.DatabaseSessionService (or set
DATABASE_URL) before importing agent themselves — whichever test file
Python imports agent from *first* in a given process decides what that
singleton is for every test that runs afterward. Patching the function
here keeps this file's behavior independent of collection order.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import google.adk.sessions as _adk_sessions
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession


class _StubSessionService:  # noqa: D401 - test stub
    """Prevents app.bot.agent's module-level DatabaseSessionService() build
    from attempting a real Postgres connection at import time, in case this
    file is collected before any other file that applies the same stub
    (see test_short_reply_context_flow.py). Every test in this file
    replaces the resulting singleton's behavior anyway via
    _patch_new_session, so the stub's own (lack of) behavior never matters
    here."""

    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from google.adk.sessions import Session  # noqa: E402

from app.bot import agent  # noqa: E402
from app.bot.whatsapp_templates import send_template_and_record  # noqa: E402
from app.models import CampaignContext  # noqa: E402


def _fresh_session(phone: str) -> Session:
    return Session(
        id="test_session", app_name="wishkr", user_id=phone,
        state={}, events=[], last_update_time=time.time(),
    )


def _patch_new_session(phone: str):
    """Patch _get_or_create_session to always report a brand-new session."""
    return patch(
        "app.bot.agent._get_or_create_session",
        new=AsyncMock(return_value=(_fresh_session(phone), True)),
    )


@pytest.mark.asyncio
async def test_template_send_then_hi_uses_campaign_specific_greeting(db: SAAsyncSession):
    phone = "919876500001"

    with patch(
        "app.services.whatsapp.whatsapp_service.send_template",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
    ) as mock_send:
        await send_template_and_record(
            db, phone,
            template_name="welcome_problem_duplicate",
            body_params=["there"],
        )
        await db.commit()
        mock_send.assert_awaited_once()

    ctx = (await db.execute(
        select(CampaignContext).where(CampaignContext.phone == phone)
    )).scalar_one()
    assert ctx.template_name == "welcome_problem_duplicate"
    assert ctx.consumed_at is None

    with _patch_new_session(phone), patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        reply = await agent.handle_message(db, phone, "Hi")
        mock_get_runner.assert_not_called()

    assert "avoid buying the same gift" in reply
    assert reply.endswith("what should I call you?")

    await db.refresh(ctx)
    assert ctx.consumed_at is not None


@pytest.mark.asyncio
async def test_hi_without_campaign_context_uses_default_greeting(db: SAAsyncSession):
    phone = "919876500002"

    with _patch_new_session(phone), patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        reply = await agent.handle_message(db, phone, "Hi")
        mock_get_runner.assert_not_called()

    assert "I'm Wishkr" in reply
    assert "what should I call you?" in reply


@pytest.mark.asyncio
async def test_hi_consumes_campaign_context_so_it_cannot_be_replayed(db: SAAsyncSession):
    """The campaign row is marked consumed on first use, per-phone, once.

    Repeated-"hi" replay behavior (not re-showing campaign copy on a
    second greeting) is covered at the unit level in
    test_campaign_context_greeting.py::test_already_consumed_context_is_not_replayed.
    """
    phone = "919876500003"

    with patch(
        "app.services.whatsapp.whatsapp_service.send_template",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.test2"}]}),
    ):
        await send_template_and_record(
            db, phone,
            template_name="welcome_problem_gifts",
            body_params=["there"],
        )
        await db.commit()

    with _patch_new_session(phone), patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        first_reply = await agent.handle_message(db, phone, "Hi")
        mock_get_runner.assert_not_called()
    assert "gift decisions less confusing" in first_reply

    ctx = (await db.execute(
        select(CampaignContext).where(CampaignContext.phone == phone)
    )).scalar_one()
    assert ctx.consumed_at is not None
    assert ctx.template_name == "welcome_problem_gifts"
