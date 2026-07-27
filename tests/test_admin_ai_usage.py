from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_ai_usage import get_ai_assistant_user, list_ai_assistant_users
from app.db.base import Base
from app.models.conversation import ConversationFlow, ConversationState
from app.models.conversation_message import ConversationMessage, MessageRole
from app.models.llm_usage_event import LlmUsageEvent
from app.models.user import User
from app.utils.phone_hash import hash_phone


@pytest.fixture
async def ai_usage_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                ConversationMessage.__table__,
                ConversationState.__table__,
                LlmUsageEvent.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    db.add(User(id="usr_1", phone="9999999999", name="Asha Rao"))
    db.add(User(id="usr_2", phone="8888888888", name="Ravi Kumar"))  # never messaged

    db.add(
        ConversationMessage(
            phone="9999999999", role=MessageRole.USER, content="hi", created_at=now - timedelta(minutes=5)
        )
    )
    db.add(
        ConversationMessage(
            phone="9999999999", role=MessageRole.MODEL, content="hello!", created_at=now
        )
    )
    db.add(ConversationState(phone="9999999999", flow=ConversationFlow.BROWSING_GIFTS))
    db.add(
        LlmUsageEvent(
            id="evt_1",
            provider="gemini",
            model="gemini-2.5-flash",
            purpose="main_agent",
            phone_hash=hash_phone("9999999999"),
            total_tokens=150,
            estimated_cost_usd=0.0025,
        )
    )
    await db.commit()


async def test_list_ai_assistant_users_aggregates_messages(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        result = await list_ai_assistant_users(db=db, q=None, page=1, limit=100)

    assert result["total"] == 1
    user = result["users"][0]
    assert user["user_id"] == "usr_1"
    assert user["message_count"] == 2
    assert user["current_flow"] == "BROWSING_GIFTS"


async def test_list_ai_assistant_users_excludes_users_with_no_messages(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        result = await list_ai_assistant_users(db=db, q=None, page=1, limit=100)

    ids = {u["user_id"] for u in result["users"]}
    assert "usr_2" not in ids


async def test_list_ai_assistant_users_filters_by_query(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        result = await list_ai_assistant_users(db=db, q="asha", page=1, limit=100)
        assert len(result["users"]) == 1

        result = await list_ai_assistant_users(db=db, q="nonexistent", page=1, limit=100)
        assert len(result["users"]) == 0


async def test_get_ai_assistant_user_returns_messages_and_usage(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        result = await get_ai_assistant_user(user_id="usr_1", db=db, message_limit=100)

    assert result["message_count"] == 2
    assert result["messages"][0]["content"] == "hi"
    assert result["messages"][1]["content"] == "hello!"
    assert result["usage"]["llm_call_count"] == 1
    assert result["usage"]["total_tokens"] == 150
    assert result["usage"]["total_cost_usd"] == pytest.approx(0.0025)


async def test_list_ai_assistant_users_includes_unregistered_chatters(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        db.add(
            ConversationMessage(
                phone="7777777777",
                role=MessageRole.USER,
                content="hi",
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

        result = await list_ai_assistant_users(db=db, q=None, page=1, limit=100)

    row = next(u for u in result["users"] if u["phone"] == "7777777777")
    assert result["total"] == 2
    assert row["user_id"] == "7777777777"
    assert row["name"] is None
    assert row["message_count"] == 1


async def test_get_ai_assistant_user_reports_total_count_when_messages_are_limited(
    ai_usage_session_factory,
):
    async with ai_usage_session_factory() as db:
        await _seed(db)
        result = await get_ai_assistant_user(user_id="usr_1", db=db, message_limit=1)

    assert result["message_count"] == 2
    assert len(result["messages"]) == 1


async def test_get_ai_assistant_user_orders_same_timestamp_messages_by_id(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        db.add(User(id="usr_3", phone="6666666666", name="Tie Break"))
        same_time = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)
        db.add(
            ConversationMessage(
                id=10,
                phone="6666666666",
                role=MessageRole.USER,
                content="question",
                created_at=same_time,
            )
        )
        db.add(
            ConversationMessage(
                id=11,
                phone="6666666666",
                role=MessageRole.MODEL,
                content="answer",
                created_at=same_time,
            )
        )
        await db.commit()

        result = await get_ai_assistant_user(user_id="usr_3", db=db, message_limit=100)

    assert [m["content"] for m in result["messages"]] == ["question", "answer"]


async def test_get_ai_assistant_user_matches_normalized_whatsapp_phone(ai_usage_session_factory):
    async with ai_usage_session_factory() as db:
        db.add(User(id="usr_4", phone="+91 99700 73324", name="Web Buyer"))
        db.add(
            ConversationMessage(
                phone="919970073324",
                role=MessageRole.USER,
                content="whatsapp hi",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            LlmUsageEvent(
                id="evt_2",
                provider="gemini",
                model="gemini-2.5-flash",
                purpose="main_agent",
                phone_hash=hash_phone("919970073324"),
                total_tokens=25,
                estimated_cost_usd=0.001,
            )
        )
        await db.commit()

        result = await get_ai_assistant_user(user_id="usr_4", db=db, message_limit=100)

    assert result["user"]["phone"] == "919970073324"
    assert result["message_count"] == 1
    assert result["usage"]["llm_call_count"] == 1
    assert result["usage"]["total_tokens"] == 25
