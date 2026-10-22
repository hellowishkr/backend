"""Regression tests for short replies inside an ongoing bot conversation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Awaitable, Callable, TypeVar

import google.adk.sessions as _adk_sessions
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import agent  # noqa: E402
from app.bot.intent_classifier import IntentResult
from app.bot.state_utils import get_state
from app.db.base import Base
from app.models import (
    CelebrantFriend,
    CelebrantOccasion,
    ConversationState,
    Occasion,
    OccasionType,
    User,
    WishlistItem,
)


T = TypeVar("T")


def _run_db(fn: Callable[[object], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [
            User.__table__,
            Occasion.__table__,
            CelebrantOccasion.__table__,
            CelebrantFriend.__table__,
            ConversationState.__table__,
            WishlistItem.__table__,
        ]
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))

        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def _fake_session_with_model_text(text: str | None):
    events = []
    if text is not None:
        events.append(SimpleNamespace(
            content=SimpleNamespace(
                role="model",
                parts=[SimpleNamespace(text=text)],
            )
        ))
    return SimpleNamespace(events=events)


def test_short_yes_uses_previous_question_instead_of_cold_starting():
    session = _fake_session_with_model_text(
        "I found Amazon and Myntra. Should I create the payment link for Amazon ₹1000?"
    )

    hint = agent._intent_hint(IntentResult("affirmative", 0.95, "rule"), session)

    assert "AFFIRMATIVE to your previous message" in hint
    assert "Amazon ₹1000" in hint
    assert "Proceed on that" in hint


def test_short_yes_without_history_asks_clarifier_not_fresh_greeting():
    hint = agent._intent_hint(
        IntentResult("affirmative", 0.95, "rule"),
        _fake_session_with_model_text(None),
    )

    assert "prior question is no longer in this session" in hint
    assert "Ask ONE short clarifier" in hint
    assert "DO NOT greet" in hint


def test_greeting_mid_conversation_restates_last_question():
    session = _fake_session_with_model_text(
        "Do you want me to remind Asha and Rohit, or only Asha?"
    )

    hint = agent._intent_hint(IntentResult("greeting", 0.95, "rule"), session)

    assert "possibly CONFUSED" in hint
    assert "Do you want me to remind Asha and Rohit" in hint
    assert "DO NOT cold-start" in hint






def test_load_user_context_preserves_short_checkout_and_friend_context():
    async def scenario(db):
        phone = "919777777777"
        user = User(id="usr_context", phone=phone, name="Buyer")
        owner = User(id="usr_context_owner", phone="919777777778", name="Owner")
        occasion = Occasion(
            id="occ_friend_context",
            user_id=owner.id,
            contact_id=None,
            type=OccasionType.BIRTHDAY,
            date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            recurring=True,
            # Owner-facing deletion must not invalidate an already-sent
            # friend reminder; the retained rows intentionally still resolve.
            deleted_at=datetime.now(timezone.utc),
        )
        celebrant = CelebrantOccasion(
            id="cel_friend_context",
            occasion_id=occasion.id,
        )
        friend = CelebrantFriend(
            id="cfr_friend_context",
            celebrant_occasion_id=celebrant.id,
            phone=phone,
            user_id=user.id,
            name="Buyer",
        )
        db.add_all([user, owner, occasion, celebrant, friend])
        db.add(ConversationState(
            phone=phone,
            temp_data=json.dumps({
                "celebrant_occasion_id": "cel_friend_context",
                "cart_created_at": datetime.now(timezone.utc).isoformat(),
                "cart_items": [
                    {"brand_name": "Amazon", "amount": 1000},
                    {"brand_name": "Myntra", "amount": 500},
                ],
            }),
        ))
        await db.flush()
        return await agent._load_user_context(db, phone)

    user, celebrant_occasion_id, cart_count = _run_db(scenario)

    assert user is not None
    assert user.id == "usr_context"
    assert celebrant_occasion_id == "cel_friend_context"
    assert cart_count == 2
    assert agent._pick_phase(user, celebrant_occasion_id, cart_count) == "friend"


def test_stale_reminder_context_does_not_trap_continue_gift_plan():
    """The reported Alisha/Karan flow must return to normal gift planning."""

    async def scenario(db):
        phone = "919777777779"
        user = User(id="usr_alisha", phone=phone, name="Alisha")
        db.add(user)
        db.add(ConversationState(
            phone=phone,
            temp_data=json.dumps({
                "celebrant_occasion_id": "cel_deleted_old_reminder",
                "budget": {
                    "min": 1000,
                    "max": 3000,
                    "unlimited": False,
                },
            }),
        ))
        await db.flush()

        loaded = await agent._load_user_context(db, phone)
        state = await get_state(db, phone)
        return loaded, state

    (user, celebrant_occasion_id, cart_count), state = _run_db(scenario)

    assert user is not None
    assert user.name == "Alisha"
    assert celebrant_occasion_id is None
    assert cart_count == 0
    assert agent._pick_phase(user, celebrant_occasion_id, cart_count) == "browse"
    assert "celebrant_occasion_id" not in state
    assert state["budget"]["min"] == 1000
    assert state["budget"]["max"] == 3000
