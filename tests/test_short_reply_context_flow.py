"""Regression tests for short replies inside an ongoing bot conversation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Awaitable, Callable, TypeVar

import google.adk.sessions as _adk_sessions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.bot import action_router, agent  # noqa: E402
from app.bot.canned_responses import WISHLIST_ADDED_RESPONSE, WISHLIST_AFFIRMATIVE_REPROMPT
from app.bot.intent_classifier import IntentResult
from app.bot.state_utils import get_state, set_state
from app.db.base import Base
from app.models import ConversationState, User, WishlistItem


T = TypeVar("T")


def _run_db(fn: Callable[[object], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [
            User.__table__,
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


def test_short_yes_while_awaiting_wishlist_link_reprompts_for_link(monkeypatch):
    messages: list[str | None] = []

    async def fake_append_message(_db, _phone, _role, content=None):
        messages.append(content)

    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    async def scenario(db):
        phone = "919555555555"
        user = User(id="usr_short_reply", phone=phone, name="Karan")
        db.add(user)
        await db.flush()
        await set_state(db, phone, "awaiting", "wishlist_link")

        reply = await action_router.try_handle_actions(
            db,
            phone,
            "yes",
            user=user,
            intent_result=IntentResult("affirmative", 0.95, "rule"),
        )
        state = await get_state(db, phone)
        return reply, state

    reply, state = _run_db(scenario)

    assert reply == WISHLIST_AFFIRMATIVE_REPROMPT
    assert state["awaiting"] == "wishlist_link"
    assert messages == ["yes", WISHLIST_AFFIRMATIVE_REPROMPT]


def test_short_link_reply_in_friend_context_saves_as_gift_not_self(monkeypatch):
    async def fake_append_message(*_args, **_kwargs):
        return None

    monkeypatch.setattr(action_router, "append_message", fake_append_message)

    async def scenario(db):
        phone = "919666666666"
        reply = await action_router.try_handle_product_urls_before_classifier(
            db,
            phone,
            "https://www.amazon.in/dp/B0TEST1234",
            celebrant_occasion_id="cel_birthday",
        )
        item = (
            await db.execute(select(WishlistItem).where(WishlistItem.user_id.is_not(None)))
        ).scalar_one()
        return reply, item

    reply, item = _run_db(scenario)

    assert reply == WISHLIST_ADDED_RESPONSE
    assert item.purpose == "gift"
    assert item.recipient_occasion_id == "cel_birthday"


def test_load_user_context_preserves_short_checkout_and_friend_context():
    async def scenario(db):
        phone = "919777777777"
        user = User(id="usr_context", phone=phone, name="Buyer")
        db.add(user)
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
