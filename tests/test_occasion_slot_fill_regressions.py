"""Regressions from the Shubhu/Nandakishor conversation post-mortem.

The user was adding their brother's birthday. Three consecutive replies to the
bot's own questions ("Birthday", "Brother", "Nandkishor, birhtday") were
classified `show_occasions` by the history-free LLM classifier and answered with
"I don't have any occasions saved for you yet" — the deterministic router
returned before the agent (which does have history) ever ran.

Root cause: no pending-slot state existed for the occasion-add flow. `awaiting`
had exactly one writer (the wishlist gate), so nothing recorded that the agent
had asked a question.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Awaitable, Callable, TypeVar

import google.adk.sessions as _adk_sessions
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import action_router, agent  # noqa: E402
from app.bot.intent_classifier import IntentResult  # noqa: E402
from app.bot.language_map import looks_like_occasion_list_request  # noqa: E402
from app.bot.state_utils import (  # noqa: E402
    AWAITING_OCCASION_SLOTS,
    clear_occasion_slots,
    get_awaiting,
    get_occasion_slots,
    remember_occasion_slots,
)
from app.db.base import Base  # noqa: E402
from app.models import ConversationState, User, WishlistItem  # noqa: E402

T = TypeVar("T")


def _run_db(fn: Callable[[object], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [User.__table__, ConversationState.__table__, WishlistItem.__table__]
        async with engine.begin() as conn:
            await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


# --- F1/F2: the router must not hijack a reply to its own question ----------


def _armed_scenario(text: str, intent: str = "show_occasions"):
    async def scenario(db):
        phone = "919000000001"
        user = User(id="usr_slotfill", phone=phone, name="Shubhu")
        db.add(user)
        await db.flush()
        # Agent just asked "what's your brother's name and what occasion?"
        await remember_occasion_slots(
            db, phone, {"relationship": "brother", "occasion_type": "BIRTHDAY"}
        )
        reply = await action_router.try_handle_actions(
            db, phone, text,
            user=user,
            intent_result=IntentResult(intent, 0.75, "llm"),
        )
        return reply

    return _run_db(scenario)


def test_bare_occasion_word_while_awaiting_slots_is_not_hijacked():
    # "Birthday" at 9:50pm — must fall through to the agent, not dump the list.
    assert _armed_scenario("Birthday") is None


def test_name_and_occasion_reply_while_awaiting_slots_is_not_hijacked():
    # "Nandkishor, birhtday" at 9:51pm — the answer to the bot's own question.
    assert _armed_scenario("Nandkishor, birhtday") is None


def test_relation_reply_while_awaiting_slots_is_not_hijacked():
    assert _armed_scenario("Brother") is None


def test_explicit_list_request_still_works_while_awaiting_slots(monkeypatch):
    """The suppression must not swallow a genuine, unambiguous list request."""
    async def fake_get_my_occasions(_db, _phone, _args):
        return {"occasions": []}

    async def fake_append_message(*_a, **_kw):
        return None

    monkeypatch.setattr(action_router, "get_my_occasions", fake_get_my_occasions)
    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    reply = _armed_scenario("show my occasions")
    assert reply is not None
    assert "don't have any occasions saved" in reply


def test_list_request_works_normally_when_not_awaiting_slots(monkeypatch):
    async def fake_get_my_occasions(_db, _phone, _args):
        return {"occasions": []}

    async def fake_append_message(*_a, **_kw):
        return None

    monkeypatch.setattr(action_router, "get_my_occasions", fake_get_my_occasions)
    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    async def scenario(db):
        phone = "919000000002"
        user = User(id="usr_nofill", phone=phone, name="Shubhu")
        db.add(user)
        await db.flush()
        return await action_router.try_handle_actions(
            db, phone, "birthday",
            user=user,
            intent_result=IntentResult("show_occasions", 0.75, "llm"),
        )

    # No slots armed → unchanged legacy behaviour.
    assert _run_db(scenario) is not None


# --- F3: partial facts must survive across the clarifying question ----------


def test_slots_merge_and_do_not_clobber_earlier_relation():
    async def scenario(db):
        phone = "919000000003"
        await remember_occasion_slots(db, phone, {"relationship": "brother"})
        # Two turns later the name arrives — relation must survive.
        await remember_occasion_slots(db, phone, {"contact_name": "Nandakishor"})
        return await get_occasion_slots(db, phone), await get_awaiting(db, phone)

    slots, awaiting = _run_db(scenario)
    assert slots["relationship"] == "brother"
    assert slots["contact_name"] == "Nandakishor"
    assert awaiting == AWAITING_OCCASION_SLOTS


def test_clearing_slots_releases_the_awaiting_marker():
    async def scenario(db):
        phone = "919000000004"
        await remember_occasion_slots(db, phone, {"relationship": "brother"})
        await clear_occasion_slots(db, phone)
        return await get_occasion_slots(db, phone), await get_awaiting(db, phone)

    slots, awaiting = _run_db(scenario)
    assert slots == {}
    assert awaiting is None


def test_clearing_slots_leaves_an_unrelated_awaiting_marker_alone():
    """clear_occasion_slots must not stomp the wishlist gate's own marker."""
    from app.bot.state_utils import set_state

    async def scenario(db):
        phone = "919000000005"
        await set_state(db, phone, "awaiting", "wishlist_link")
        await clear_occasion_slots(db, phone)
        return await get_awaiting(db, phone)

    assert _run_db(scenario) == "wishlist_link"


# --- Typos are NOT handled by phrase lists ----------------------------------


def test_misspelled_input_is_left_to_the_agent_not_a_typo_list():
    """Deliberately no typo normalisation for "birhtday" & co.

    Hardcoding misspellings is unbounded whack-a-mole and English-only. A
    misspelled message simply isn't a deterministic list request, so it falls
    through to the agent, which reads it in context. This test pins that
    intent: if someone adds a typo table here, it should fail.
    """
    assert looks_like_occasion_list_request("which birhtdays did i save") is False


# --- F7: "ok" after a statement must not dead-end ---------------------------


def _session_with_model_text(text: str):
    return SimpleNamespace(events=[
        SimpleNamespace(content=SimpleNamespace(
            role="model", parts=[SimpleNamespace(text=text)],
        ))
    ])


def test_ok_after_a_statement_pushes_delivery_not_a_pleasantry():
    session = _session_with_model_text(
        "I'm looking for cricket bat options for Nandakishor within your ₹2000 budget."
    )
    hint = agent._intent_hint(IntentResult("affirmative", 0.95, "rule"), session)

    assert "ACKNOWLEDGEMENT" in hint
    assert "waiting on YOU" in hint
    assert "Proceed on that" not in hint


def test_yes_after_a_real_question_keeps_the_proceed_hint():
    session = _session_with_model_text("Should I save Nandakishor's birthday?")
    hint = agent._intent_hint(IntentResult("affirmative", 0.95, "rule"), session)

    assert "AFFIRMATIVE to your previous message" in hint
    assert "Proceed on that" in hint


# --- F4/F6: ambiguous dates and birth years ---------------------------------

import pytest  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.bot.tools.tool_impl import occasions  # noqa: E402


def _no_reminders(monkeypatch):
    monkeypatch.setattr(
        occasions, "schedule_reminders",
        AsyncMock(return_value={
            "reminders_created": 0, "days_until": 0, "tight_window": False,
        }),
    )


@pytest.mark.asyncio
async def test_all_digit_date_with_both_parts_under_13_asks_to_confirm(
    db, monkeypatch,
):
    """03/10/2000 could be 3 Oct or 10 Mar — never guess silently."""
    _no_reminders(monkeypatch)
    phone = "919000000010"
    db.add(User(id="usr_ambig", phone=phone, name="Shubhu"))
    await db.flush()

    result = await occasions.add_occasion(db, phone, {
        "contact_name": "Nandakishor",
        "occasion_type": "BIRTHDAY",
        "date_day": 3, "date_month": 10, "date_year": 2000,
        "date_input_numeric": True,
    })

    assert result["error"] == "date_ambiguous"
    assert "3 October" in result["message"]
    assert "March" in result["message"]


@pytest.mark.asyncio
async def test_spelled_out_month_saves_without_a_confirm_prompt(db, monkeypatch):
    """'3rd October' is unambiguous even though both parts are <= 12."""
    _no_reminders(monkeypatch)
    phone = "919000000011"
    db.add(User(id="usr_spelled", phone=phone, name="Shubhu"))
    await db.flush()

    result = await occasions.add_occasion(db, phone, {
        "contact_name": "Nandakishor",
        "occasion_type": "BIRTHDAY",
        "date_day": 3, "date_month": 10, "date_year": 2000,
    })

    assert result.get("success") is True


@pytest.mark.asyncio
async def test_unambiguous_day_over_12_never_prompts(db, monkeypatch):
    _no_reminders(monkeypatch)
    phone = "919000000012"
    db.add(User(id="usr_day25", phone=phone, name="Shubhu"))
    await db.flush()

    result = await occasions.add_occasion(db, phone, {
        "contact_name": "Asha",
        "occasion_type": "BIRTHDAY",
        "date_day": 25, "date_month": 3,
        "date_input_numeric": True,
    })

    assert result.get("success") is True


@pytest.mark.asyncio
async def test_birth_year_rolls_to_next_future_occurrence_not_year_plus_one(
    db, monkeypatch,
):
    """A 2000 birth year must not schedule reminders in 2001."""
    _no_reminders(monkeypatch)
    phone = "919000000013"
    db.add(User(id="usr_birthyear", phone=phone, name="Shubhu"))
    await db.flush()

    result = await occasions.add_occasion(db, phone, {
        "contact_name": "Nandakishor",
        "occasion_type": "BIRTHDAY",
        "date_day": 3, "date_month": 10, "date_year": 2000,
        "date_confirmed": True,
    })

    assert result["success"] is True
    saved_year = int(result["date"].split()[-1])
    assert saved_year >= datetime.now(timezone.utc).year
    # Birth year is surfaced for age-aware gifting even though it isn't stored.
    assert result["birth_year"] == 2000
    assert result["age_turning"] == saved_year - 2000
