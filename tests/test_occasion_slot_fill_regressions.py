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
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import action_router, agent  # noqa: E402
from app.bot.intent_classifier import IntentResult  # noqa: E402
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


def _exact_phrase_scenario(text: str):
    """Run one message through the zero-LLM exact-phrase table."""
    async def scenario(db):
        phone = "919000000001"
        user = User(id="usr_slotfill", phone=phone, name="Shubhu")
        db.add(user)
        await db.flush()
        return await action_router.try_handle_exact_phrases(
            db, phone, text, user=user,
        )

    return _run_db(scenario)


@pytest.mark.parametrize(
    "text",
    [
        # The Shubhu/Nandakishor turns.
        "Birthday",
        "Brother",
        "Nandkishor, birhtday",
        # The 2026-08 "DJ" transcript — six occasions lost across these.
        "Myra - birthday - 7th Aug",
        "Uttu, birthday, 9th Aug",
        "Angelina- 7th Aug- Anniversary",
        "Disha, birthday, 8th April",
        "Rishabh, 15 October",
        "Friend name - Angel, birthday- 7th Aught",
    ],
)
def test_replies_and_saves_are_never_answered_by_a_canned_list(text: str):
    """No message carrying occasion DETAILS may hit the canned list reply.

    Each of these is either an answer to a question we just asked or an
    outright save request. The old keyword path matched on the word "birthday"
    and answered "I don't have any occasions saved for you yet" — while
    occasions existed — instead of saving. They must now fall through to the
    intent router, which reads the conversation.
    """
    assert _exact_phrase_scenario(text) is None, (
        f"{text!r} was answered by the deterministic path; it must reach the agent"
    )


def test_explicit_list_request_still_answers_without_an_llm(monkeypatch):
    """A genuine, unambiguous list request stays on the zero-LLM path."""
    async def fake_get_my_occasions(_db, _phone, _args):
        return {"occasions": []}

    async def fake_append_message(*_a, **_kw):
        return None

    monkeypatch.setattr(action_router, "get_my_occasions", fake_get_my_occasions)
    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    reply = _exact_phrase_scenario("show my occasions")
    assert reply is not None
    assert "don't have any occasions saved" in reply


def test_bare_ambiguous_word_is_not_treated_as_a_list_request(monkeypatch):
    """"birthday" alone is ambiguous — the agent decides, not a keyword match."""
    async def fake_get_my_occasions(_db, _phone, _args):
        return {"occasions": []}

    async def fake_append_message(*_a, **_kw):
        return None

    monkeypatch.setattr(action_router, "get_my_occasions", fake_get_my_occasions)
    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    assert _exact_phrase_scenario("birthday") is None


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
    misspelled message simply isn't an exact-phrase match, so it falls through
    to the intent router, which reads it in context. This test pins that
    intent: if someone adds a typo table, it should fail.
    """
    assert _exact_phrase_scenario("which birhtdays did i save") is None


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


# --- Next-occurrence dates must not read as a wrong save --------------------


def test_recurring_next_year_date_is_labelled_not_bare():
    """Disha's birthday (4 Aug), listed on 6 Aug, correctly resolves to 2027.

    Shown bare, "04 August 2027" reads as though we saved the wrong year — the
    user gave "4th Aug" (2026-08 production report). The date stays accurate;
    it just gets named as the upcoming occurrence.
    """
    from app.bot.action_router import _format_my_occasions_reply

    reply = _format_my_occasions_reply({"occasions": [
        {"whose": "Disha", "occasion": "Birthday",
         "date": "04 August 2027", "recurs_on": "04 August", "recurring": True},
    ]})
    assert "04 August 2027" in reply
    assert "(every 04 August)" in reply


def test_recurrence_label_does_not_depend_on_the_current_year():
    """No "is it this year?" check — that is off by one around new year.

    Stored dates are user-local while `now` is UTC, so a year comparison
    flips a day early/late for users east of UTC. The label is unconditional
    for recurring occasions.
    """
    from app.bot.action_router import _format_my_occasions_reply

    for year in ("2026", "2027", "2099"):
        reply = _format_my_occasions_reply({"occasions": [
            {"whose": "Niharika", "occasion": "Birthday",
             "date": f"07 August {year}", "recurs_on": "07 August",
             "recurring": True},
        ]})
        assert "(every 07 August)" in reply


def test_non_recurring_occasion_is_never_labelled():
    """A one-off has no "next" occurrence — the label would be nonsense."""
    from app.bot.action_router import _format_my_occasions_reply

    reply = _format_my_occasions_reply({"occasions": [
        {"whose": "Ravi", "occasion": "Farewell",
         "date": "20 September 2027", "recurring": False},
    ]})
    assert "every" not in reply
