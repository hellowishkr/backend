"""Customer transcript regressions: persisted outcomes, ownership and safe replies."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import google.adk.sessions as adk_sessions
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _SessionStub:
    def __init__(self, **kwargs):
        pass


adk_sessions.DatabaseSessionService = _SessionStub

from app.api.product_orders import CustomerPayload, get_or_create_customer
from app.bot import agent
from app.bot.gift_context import GiftNeedProfile
from app.bot.intent_classifier import IntentResult
from app.bot.order_support import get_order_support
from app.bot.sources.base import GiftCandidate
from app.bot.state_utils import get_state, remember_budget, update_active_gift_context
from app.bot.tools.tool_impl.users import register_user
from app.bot.tools.tool_impl.reminders import set_reminder_schedule
from app.db.base import Base
from app.models import City, Contact, ConversationMessage, ConversationState, Occasion, OccasionType
from app.models import Order, OrderStatus, Reminder, User, Vendor, OccasionGiftPlan
from app.services.gift_recommendation import (
    GiftRecommendationRequest,
    _candidate_allowed,
    _idea_suggestions,
    _recommendation_context,
)
from app.services.reminder import schedule_reminders
from app.utils.phones import normalize_phone, user_for_phone


@pytest.fixture
async def chat_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        m.__table__
        for m in (
            City,
            User,
            Vendor,
            Order,
            Contact,
            Occasion,
            Reminder,
            ConversationMessage,
            ConversationState,
            OccasionGiftPlan,
        )
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()


async def _buyer(db):
    db.add(User(id="guest", phone="+919000000001", name="Buyer", is_guest=True))
    db.add(User(id="registered", phone="919000000001", name="Buyer", is_guest=False))
    await db.flush()
    db.add(
        Order(
            id="ord_existing", user_id="guest", total_amount=Decimal("999"), status=OrderStatus.PAID
        )
    )
    await db.flush()


async def test_poojith_return_support_is_owned_structured_data(chat_db):
    await _buyer(chat_db)
    result = await get_order_support(chat_db, "919000000001", {"order_id": "ord_existing"})
    assert result["orders"][0]["status"] == "PAID"
    assert result["return_eligibility"] == "requires_team_review"
    assert not result["order_changed"]
    assert (await chat_db.get(Order, "ord_existing")).status == OrderStatus.PAID
    assert await chat_db.scalar(select(func.count()).select_from(User)) == 2


async def test_support_never_discloses_another_buyers_order(chat_db):
    await _buyer(chat_db)
    result = await get_order_support(chat_db, "919000000002", {"order_id": "ord_existing"})
    assert result["orders"] == [] and not result["order_verified"]
    assert "PAID" not in str(result) and "999" not in str(result)


async def test_checkout_reuses_registered_phone_alias(chat_db):
    await _buyer(chat_db)
    payload = CustomerPayload(name="Buyer", phone="+91 90000 00001")
    assert payload.phone == "919000000001"
    user = await get_or_create_customer(chat_db, payload)
    assert user.id == "registered"
    assert (await user_for_phone(chat_db, "09000000001")).id == "registered"
    assert await chat_db.scalar(select(func.count()).select_from(User)) == 2


async def test_guest_registration_keeps_checkout_identity(chat_db):
    chat_db.add(User(id="guest", phone="+919000000001", name="Buyer", is_guest=True))
    await chat_db.flush()
    result = await register_user(chat_db, "919000000001", {"name": "Poojith"})
    assert result["success"]
    user = await chat_db.get(User, "guest")
    assert user.name == "Poojith" and not user.is_guest
    assert await chat_db.scalar(select(func.count()).select_from(User)) == 1


async def test_shree_name_correction_is_persisted(chat_db):
    chat_db.add(User(id="user", phone="919000000001", name="Saadgee", is_guest=False))
    await chat_db.flush()
    await register_user(chat_db, "919000000001", {"name": "Shree"})
    assert (await chat_db.get(User, "user")).name == "Shree"




async def test_old_birth_year_can_be_rescheduled_into_future(chat_db):
    user = User(id="user", phone="919000000001", name="Shubhu")
    chat_db.add(user)
    await chat_db.flush()
    occ = Occasion(
        id="occ_old",
        user_id=user.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2001, 10, 3, tzinfo=timezone.utc),
        recurring=True,
    )
    chat_db.add(occ)
    await chat_db.flush()
    result = await schedule_reminders(chat_db, occ)
    assert occ.date.year >= datetime.now(timezone.utc).year
    assert result["reminders_created"] > 0
    assert all(
        r.scheduled_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        for r in await chat_db.scalars(select(Reminder))
    )


async def test_manya_unrestricted_budget_clears_old_limit(chat_db):
    phone = "919000000001"
    await update_active_gift_context(
        chat_db, phone, {"purpose": "self", "budget_max": 1000}, sync_gift_plan=False
    )
    await remember_budget(chat_db, phone, "Budget ki tention mt li")
    raw = (await get_state(chat_db, phone))["active_gift_context"]
    profile = GiftNeedProfile.from_mapping(raw)
    assert profile.budget_unlimited and profile.budget_max is None
    _, missing, question = _recommendation_context(
        GiftRecommendationRequest(phone=phone, profile=profile, recipient_vibe="flowers"),
        occasion_type="BIRTHDAY",
        vibes=["flowers"],
        budget_min=None,
        budget_max=None,
    )
    assert "budget" not in missing and question is None






def test_empty_gift_reply_cannot_leak_internal_instructions():
    reply = agent._salvage_reply_from_tool(
        "suggest_gifts",
        {
            "success": True,
            "message": "No deterministic match was found. Never ask known_fields.",
            "suggestions": [],
        },
    )
    assert "known_fields" not in reply and "deterministic" not in reply


def test_abha_exclusions_and_vasanth_requested_category_are_hard_filters():
    candidate = GiftCandidate(
        source_id="vendor_db",
        item_id="flowers",
        name="Chocolate flower hamper",
        vibes=["flowers"],
        price_min=3500,
        price_max=3500,
        fulfillment="physical_delivery",
        metadata={"category": "flowers"},
    )
    assert not _candidate_allowed(
        candidate, GiftRecommendationRequest(phone="test", desired_categories=["fashion"])
    )
    book = GiftCandidate(
        source_id="vendor_db",
        item_id="book",
        name="History books",
        vibes=["books"],
        price_min=500,
        price_max=500,
        fulfillment="physical_delivery",
    )
    assert not _candidate_allowed(
        book, GiftRecommendationRequest(phone="test", excluded_categories=["books"])
    )


def test_finished_wall_art_and_art_supplies_are_different_shortlists():
    wall = _idea_suggestions(["wall_art"], 3, recipient_interest="a unique wall art piece")
    supplies = _idea_suggestions(["wall_art"], 3, recipient_interest="art supplies")
    assert any("Framed" in p["name"] for p in wall)
    assert all("kit" not in p["name"] and "Sketchbook" not in p["name"] for p in wall)
    assert any("Sketchbook" in p["name"] for p in supplies)


def test_yes_after_session_refresh_uses_the_persisted_question():
    hint = agent._intent_hint(
        IntentResult("affirmative", 1, "rule"),
        SimpleNamespace(events=[]),
        "Would you like gift ideas for Amar under ₹1,000?",
    )
    assert "Amar" in hint and "Proceed on that" in hint


@pytest.mark.parametrize("raw", ["9000000001", "+919000000001", "09000000001", "91 90000 00001"])
def test_phone_identity_is_consistent(raw):
    assert normalize_phone(raw) == "919000000001"


async def test_netra_custom_reminder_time_is_persisted_in_user_timezone(chat_db):
    user = User(id="user", phone="919000000001", name="Netra", timezone="Asia/Kolkata")
    chat_db.add(user)
    await chat_db.flush()
    occ = Occasion(
        id="occ_time",
        user_id=user.id,
        type=OccasionType.CUSTOM,
        custom_label="Conversation",
        date=datetime(2099, 6, 20, tzinfo=timezone.utc),
        recurring=False,
    )
    chat_db.add(occ)
    await chat_db.flush()
    result = await set_reminder_schedule(
        chat_db,
        user.phone,
        {"occasion_id": occ.id, "days_before": [0], "local_hour": 15, "local_minute": 30},
    )
    assert result["success"]
    row = result["scheduled_reminders"][0]
    assert "T15:30:00+05:30" in row["scheduled_local"]
    assert "T10:00:00+00:00" in row["scheduled_at"]
    persisted = await chat_db.scalar(select(Reminder).where(Reminder.occasion_id == occ.id))
    assert persisted.scheduled_at.hour == 10


async def test_checkout_cannot_overwrite_a_registered_profile(chat_db):
    await _buyer(chat_db)
    await get_or_create_customer(
        chat_db,
        CustomerPayload(name="Someone else", phone="+919000000001", email="unverified@example.com"),
    )
    user = await chat_db.get(User, "registered")
    assert user.name == "Buyer" and user.email is None


async def test_adk_wrappers_forward_category_filters_and_explicit_time(monkeypatch):
    from unittest.mock import AsyncMock
    from app.bot.tools import adapter

    execute = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(adapter, "execute_tool", execute)
    await adapter.suggest_gifts(
        desired_categories=["flowers"],
        excluded_categories=["books"],
        gift_format="physical",
        constraints={"style": "full luxury box"},
    )
    args = execute.call_args.args[1]
    assert args["desired_categories"] == ["flowers"]
    assert args["excluded_categories"] == ["books"]
    assert args["constraints"]["style"] == "full luxury box"
    await adapter.set_reminder_schedule("occ_test", [1, 0], local_hour=15, local_minute=0)
    assert execute.call_args.args[1]["local_hour"] == 15
    assert execute.call_args.args[1]["local_minute"] == 0
    await adapter.set_reminder_schedule("occ_test", [1, 0])
    assert "local_hour" not in execute.call_args.args[1]


async def test_turn_date_uses_local_day_at_utc_midnight_boundary(chat_db, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 28, 22, 17, tzinfo=timezone.utc)

    monkeypatch.setattr(agent, "datetime", FrozenDateTime)
    user = User(id="user", phone="919000000001", name="Nancy", timezone="Asia/Kolkata")
    chat_db.add(user)
    await chat_db.flush()
    tail = await agent._build_dynamic_tail(chat_db, user.phone, user, None)
    assert "Today: 29 Jun 2026" in tail
    assert "2026-06-29T03:47:00+05:30" in tail
