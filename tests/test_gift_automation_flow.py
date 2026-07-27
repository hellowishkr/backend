"""Automation coverage for multi-occasion gift/reminder flows."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.tools import functions
from app.bot.tools import adapter
from app.bot.tools.tool_impl import orders as order_tools
from app.bot.tools.functions import CART_KEY, CART_CREATED_AT_KEY
from app.db.base import Base
from app.jobs import send_reminders
from app.models import (
    CelebrantFriend,
    CelebrantOccasion,
    Contact,
    ConversationState,
    DeliveryStatus,
    ExternalGiftClaim,
    GiftType,
    Occasion,
    OccasionType,
    Order,
    OrderStatus,
    Purchase,
    PurchaseStatus,
    Reminder,
    ReminderStatus,
    User,
    WishlistItem,
)
from app.services.reminder import (
    REMINDER_SCHEDULE_DAYS,
    schedule_reminders,
)
from app.services.fulfillment_policy import (
    FulfillmentPolicy,
    apply_fulfillment_defaults,
    compute_fulfillment_due_at,
)


T = TypeVar("T")


def _run_db(fn: Callable[[object], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [
            User.__table__,
            Contact.__table__,
            Occasion.__table__,
            CelebrantOccasion.__table__,
            CelebrantFriend.__table__,
            Reminder.__table__,
            ConversationState.__table__,
            Order.__table__,
            WishlistItem.__table__,
            Purchase.__table__,
            ExternalGiftClaim.__table__,
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


async def _seed_celebrant_event(
    db,
    *,
    celebrant_id: str,
    celebrant_phone: str,
    celebrant_name: str,
    occasion_id: str,
    cel_id: str,
    occasion_type: OccasionType,
    date: datetime,
    preferences: list[str],
    friend_phone: str,
    friend_name: str,
) -> tuple[Occasion, CelebrantOccasion, CelebrantFriend]:
    user = await db.get(User, celebrant_id)
    if user is None:
        user = User(id=celebrant_id, phone=celebrant_phone, name=celebrant_name)
        db.add(user)
        await db.flush()

    occasion = Occasion(
        id=occasion_id,
        user_id=user.id,
        contact_id=None,
        type=occasion_type,
        date=date,
        recurring=True,
    )
    db.add(occasion)
    await db.flush()

    cel_occ = CelebrantOccasion(
        id=cel_id,
        occasion_id=occasion.id,
        preferences=json.dumps(preferences),
    )
    db.add(cel_occ)
    friend = CelebrantFriend(
        id=f"cfr_{cel_id}_{friend_phone[-4:]}",
        celebrant_occasion_id=cel_occ.id,
        phone=friend_phone,
        name=friend_name,
    )
    db.add(friend)
    await db.flush()
    return occasion, cel_occ, friend


def test_multiple_events_keep_separate_reminders_and_preferences():
    async def scenario(db):
        birthday, cel_birthday, _ = await _seed_celebrant_event(
            db,
            celebrant_id="usr_celebrant",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_birthday",
            cel_id="cel_birthday",
            occasion_type=OccasionType.BIRTHDAY,
            # Relative date: the original hardcoded 2026-06-10 fixture broke
            # the day after it passed (schedule_reminders collapses past
            # dates into a single tight-window reminder).
            date=datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=30),
            preferences=["Gift Cards", "Books"],
            friend_phone="919222222222",
            friend_name="Asha",
        )
        anniversary, cel_anniversary, _ = await _seed_celebrant_event(
            db,
            celebrant_id="usr_celebrant",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_anniversary",
            cel_id="cel_anniversary",
            occasion_type=OccasionType.ANNIVERSARY,
            date=datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=60),
            preferences=["Experiences", "Dining"],
            friend_phone="919333333333",
            friend_name="Rohit",
        )

        birthday_sched = await schedule_reminders(db, birthday, target_phones=["919222222222"])
        anniversary_sched = await schedule_reminders(db, anniversary, target_phones=["919333333333"])
        await db.commit()

        birthday_rows = (
            await db.execute(select(Reminder).where(Reminder.occasion_id == birthday.id))
        ).scalars().all()
        anniversary_rows = (
            await db.execute(select(Reminder).where(Reminder.occasion_id == anniversary.id))
        ).scalars().all()

        ctx_birthday = await functions.get_celebrant_context(
            db, "919222222222", {"celebrant_occasion_id": cel_birthday.id}
        )
        ctx_anniversary = await functions.get_celebrant_context(
            db, "919333333333", {"celebrant_occasion_id": cel_anniversary.id}
        )
        wrong_friend = await functions.get_celebrant_context(
            db, "919222222222", {"celebrant_occasion_id": cel_anniversary.id}
        )

        return {
            "birthday_sched": birthday_sched,
            "anniversary_sched": anniversary_sched,
            "birthday_rows": birthday_rows,
            "anniversary_rows": anniversary_rows,
            "ctx_birthday": ctx_birthday,
            "ctx_anniversary": ctx_anniversary,
            "wrong_friend": wrong_friend,
        }

    out = _run_db(scenario)

    assert out["birthday_sched"]["reminders_created"] == len(REMINDER_SCHEDULE_DAYS)
    assert out["anniversary_sched"]["reminders_created"] == len(REMINDER_SCHEDULE_DAYS)
    assert {r.target_phone for r in out["birthday_rows"]} == {"919222222222"}
    assert {r.target_phone for r in out["anniversary_rows"]} == {"919333333333"}
    assert out["ctx_birthday"]["preferences"] == ["Gift Cards", "Books"]
    assert out["ctx_anniversary"]["preferences"] == ["Experiences", "Dining"]
    assert out["wrong_friend"]["error"] == "not_invited"


def test_dispatch_reminder_sets_friend_context_for_the_specific_event(monkeypatch):
    sent: list[tuple[str, str, list[str]]] = []

    async def fake_send_template(phone, *, template_name, body_params, **_kwargs):
        sent.append((phone, template_name, body_params))
        return {"messages": [{"id": f"wamid.{phone}"}]}

    monkeypatch.setattr(send_reminders.whatsapp_service, "send_template", fake_send_template)

    async def scenario(db):
        birthday, cel_birthday, friend = await _seed_celebrant_event(
            db,
            celebrant_id="usr_celebrant",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_birthday",
            cel_id="cel_birthday",
            occasion_type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            preferences=["Books"],
            friend_phone="919222222222",
            friend_name="Asha",
        )
        reminder = Reminder(
            id="rem_birthday_friend",
            occasion_id=birthday.id,
            scheduled_at=datetime.now(timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="early_reminder",
            target_phone=friend.phone,
        )
        db.add(reminder)
        await db.flush()

        result = await send_reminders.dispatch_reminder(db, reminder)
        state = await db.get(ConversationState, friend.phone)
        await db.commit()
        return result, reminder, state, cel_birthday

    result, reminder, state, cel_birthday = _run_db(scenario)

    assert result["success"] is True
    assert reminder.status == ReminderStatus.SENT
    assert reminder.whatsapp_msg_id == "wamid.919222222222"
    assert sent and sent[0][0] == "919222222222"
    assert json.loads(state.temp_data)["celebrant_occasion_id"] == cel_birthday.id


def test_targeted_reminder_without_celebrant_occasion_fails_terminally():
    async def scenario(db):
        user = User(id="usr_plain", phone="919111111111", name="Karan")
        occasion = Occasion(
            id="occ_plain",
            user_id=user.id,
            contact_id=None,
            type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        reminder = Reminder(
            id="rem_plain_friend",
            occasion_id=occasion.id,
            scheduled_at=datetime.now(timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="early_reminder",
            target_phone="919222222222",
        )
        db.add_all([user, occasion, reminder])
        await db.flush()

        result = await send_reminders.dispatch_reminder(db, reminder)
        await db.commit()
        return result, reminder

    result, reminder = _run_db(scenario)

    assert result == {
        "success": False,
        "skipped": False,
        "reminder_id": "rem_plain_friend",
        "sent_to": "919222222222",
        "message_type": "early_reminder",
        "reason": "celebrant_occasion_missing reminder=rem_plain_friend occasion=occ_plain",
    }
    assert reminder.status == ReminderStatus.FAILED
    assert reminder.last_error == "celebrant_occasion_missing reminder=rem_plain_friend occasion=occ_plain"


def test_merge_occasions_moves_dependents_and_deletes_duplicate():
    async def scenario(db):
        user = User(id="usr_owner", phone="919111111111", name="Swaraj")
        sister = Contact(id="con_sister", user_id=user.id, name="sister")
        rutuja = Contact(id="con_rutuja", user_id=user.id, name="Rutuja", phone="919222222222")
        keep = Occasion(
            id="occ_sister",
            user_id=user.id,
            contact_id=sister.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        duplicate = Occasion(
            id="occ_rutuja",
            user_id=user.id,
            contact_id=rutuja.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        order = Order(
            id="ord_rutuja",
            user_id=user.id,
            total_amount=500,
            status=OrderStatus.COMPLETED,
        )
        purchase = Purchase(
            id="pur_rutuja",
            order_id=order.id,
            occasion_id=duplicate.id,
            gift_type=GiftType.GIFT_CARD,
            brand_name="Amazon",
            amount=500,
            status=PurchaseStatus.VOUCHER_ISSUED,
        )
        reminder = Reminder(
            id="rem_rutuja",
            occasion_id=duplicate.id,
            scheduled_at=datetime(2027, 5, 5, 3, 30, tzinfo=timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="day_of_reminder",
        )
        claim = ExternalGiftClaim(
            id="ext_rutuja",
            occasion_id=duplicate.id,
            buyer_phone="919333333333",
            item_description="Book",
        )
        wishlist = WishlistItem(
            id="wsh_rutuja",
            user_id=user.id,
            url="https://example.com/gift",
            purpose="gift",
            recipient_occasion_id=duplicate.id,
        )
        db.add_all([
            user, sister, rutuja, keep, duplicate, order, purchase,
            reminder, claim, wishlist,
        ])
        await db.flush()

        result = await functions.merge_occasions(
            db,
            user.phone,
            {
                "keep_occasion_id": keep.id,
                "merge_occasion_id": duplicate.id,
                "contact_name": "Rutuja",
            },
        )
        await db.commit()
        return result, keep, sister, purchase, reminder, claim, wishlist, await db.get(Occasion, duplicate.id)

    result, keep, sister, purchase, reminder, claim, wishlist, duplicate = _run_db(scenario)

    assert result["success"] is True
    assert result["merged"] is True
    assert result["occasion_id"] == "occ_sister"
    assert duplicate is None
    assert sister.name == "Rutuja"
    assert purchase.occasion_id == keep.id
    assert reminder.occasion_id == keep.id
    assert claim.occasion_id == keep.id
    assert wishlist.recipient_occasion_id == keep.id


def test_add_occasion_renames_generic_relation_instead_of_duplicating():
    async def scenario(db):
        user = User(id="usr_owner", phone="919111111111", name="Swaraj")
        sister = Contact(id="con_sister", user_id=user.id, name="sister")
        occasion = Occasion(
            id="occ_sister",
            user_id=user.id,
            contact_id=sister.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        db.add_all([user, sister, occasion])
        await db.flush()

        result = await functions.add_occasion(
            db,
            user.phone,
            {
                "contact_name": "Rutuja",
                "occasion_type": "BIRTHDAY",
                "date_day": 5,
                "date_month": 5,
            },
        )
        occasion_count = len(
            (
                await db.execute(
                    select(Occasion).where(
                        Occasion.user_id == user.id,
                        Occasion.type == OccasionType.BIRTHDAY,
                    )
                )
            ).scalars().all()
        )
        await db.commit()
        return result, sister, occasion_count

    result, sister, occasion_count = _run_db(scenario)

    assert result["success"] is True
    assert result["reused"] is True
    assert result["merged"] is True
    assert result["occasion_id"] == "occ_sister"
    assert sister.name == "Rutuja"
    assert occasion_count == 1


def test_add_occasion_asks_for_name_when_only_generic_alias_given():
    """A bare ambiguous relation ("sister") is rejected so the user is asked
    for the person's name first — it must NOT silently merge into an unrelated
    existing occasion that happens to share the date."""
    async def scenario(db):
        user = User(id="usr_owner", phone="919111111111", name="Swaraj")
        rutuja = Contact(id="con_rutuja", user_id=user.id, name="Rutuja")
        occasion = Occasion(
            id="occ_rutuja",
            user_id=user.id,
            contact_id=rutuja.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        db.add_all([user, rutuja, occasion])
        await db.flush()

        result = await functions.add_occasion(
            db,
            user.phone,
            {
                "contact_name": "sister",
                "occasion_type": "BIRTHDAY",
                "date_day": 5,
                "date_month": 5,
            },
        )
        occasion_count = len(
            (
                await db.execute(
                    select(Occasion).where(
                        Occasion.user_id == user.id,
                        Occasion.type == OccasionType.BIRTHDAY,
                    )
                )
            ).scalars().all()
        )
        await db.commit()
        return result, rutuja, occasion_count

    result, rutuja, occasion_count = _run_db(scenario)

    assert result["error"] == "specific_contact_name_required"
    assert "name" in result["message"].lower()
    assert rutuja.name == "Rutuja"
    assert occasion_count == 1


def test_merge_occasions_deletes_duplicate_sent_reminder():
    async def scenario(db):
        user = User(id="usr_owner", phone="919111111111", name="Swaraj")
        sister = Contact(id="con_sister", user_id=user.id, name="sister")
        rutuja = Contact(id="con_rutuja", user_id=user.id, name="Rutuja")
        keep = Occasion(
            id="occ_sister",
            user_id=user.id,
            contact_id=sister.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        duplicate = Occasion(
            id="occ_rutuja",
            user_id=user.id,
            contact_id=rutuja.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2027, 5, 5, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        scheduled_at = datetime(2027, 5, 5, 3, 30, tzinfo=timezone.utc)
        keep_reminder = Reminder(
            id="rem_keep_sent",
            occasion_id=keep.id,
            scheduled_at=scheduled_at,
            sent_at=scheduled_at,
            status=ReminderStatus.SENT,
            message_type="day_of_reminder",
        )
        duplicate_reminder = Reminder(
            id="rem_dup_sent",
            occasion_id=duplicate.id,
            scheduled_at=scheduled_at,
            sent_at=scheduled_at,
            status=ReminderStatus.SENT,
            message_type="day_of_reminder",
        )
        db.add_all([user, sister, rutuja, keep, duplicate, keep_reminder, duplicate_reminder])
        await db.flush()

        result = await functions.merge_occasions(
            db,
            user.phone,
            {"keep_occasion_id": keep.id, "merge_occasion_id": duplicate.id},
        )
        remaining_sent = (
            (
                await db.execute(
                    select(Reminder).where(
                        Reminder.occasion_id == keep.id,
                        Reminder.status == ReminderStatus.SENT,
                        Reminder.message_type == "day_of_reminder",
                    )
                )
            ).scalars().all()
        )
        await db.commit()
        deleted_duplicate_reminder = await db.get(Reminder, duplicate_reminder.id)
        return result, remaining_sent, deleted_duplicate_reminder

    result, remaining_sent, deleted_duplicate_reminder = _run_db(scenario)

    assert result["cancelled_duplicate_reminders"] == 1
    assert len(remaining_sent) == 1
    assert remaining_sent[0].id == "rem_keep_sent"
    assert deleted_duplicate_reminder is None


def test_tool_precondition_blocks_targeted_reminder_before_setup():
    async def scenario(db):
        user = User(id="usr_plain", phone="919111111111", name="Karan")
        occasion = Occasion(
            id="occ_plain",
            user_id=user.id,
            contact_id=None,
            type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            recurring=True,
        )
        db.add_all([user, occasion])
        await db.flush()

        return await adapter._validate_tool_preconditions(
            "send_reminder_now",
            {"occasion_id": occasion.id, "target_phone": "919222222222"},
            db,
            user.phone,
        )

    result = _run_db(scenario)

    assert result == {
        "error": "precondition_failed",
        "reason": "target_phone requires a friend-setup row for this occasion.",
        "required_action": (
            "Call setup_celebrant_occasion first, then retry send_reminder_now "
            "with the returned occasion_id."
        ),
        "retryable": False,
        "prerequisite_tool": "setup_celebrant_occasion",
    }


def test_tool_precondition_allows_targeted_reminder_after_setup():
    async def scenario(db):
        occasion, _, friend = await _seed_celebrant_event(
            db,
            celebrant_id="usr_celebrant",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_birthday",
            cel_id="cel_birthday",
            occasion_type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            preferences=["Books"],
            friend_phone="919222222222",
            friend_name="Asha",
        )
        await db.flush()

        return await adapter._validate_tool_preconditions(
            "send_reminder_now",
            {"occasion_id": occasion.id, "target_phone": friend.phone},
            db,
            "919111111111",
        )

    assert _run_db(scenario) is None


def test_tool_precondition_blocks_friend_wishlist_without_recipient():
    async def scenario(db):
        return await adapter._validate_tool_preconditions(
            "add_to_wishlist",
            {"url": "https://example.com/product", "purpose": "gift"},
            db,
            "919111111111",
        )

    result = _run_db(scenario)

    assert result == {
        "error": "precondition_failed",
        "reason": "purpose='gift' requires recipient_occasion_id.",
        "required_action": (
            "Use purpose='self' unless FriendContext/occasion context is active; "
            "otherwise ask which occasion this gift is for."
        ),
        "retryable": False,
        "missing": ["recipient_occasion_id"],
    }


def test_tool_spec_blocks_missing_required_order_fields():
    async def scenario(db):
        return await adapter._validate_tool_preconditions(
            "create_gift_order",
            {},
            db,
            "919111111111",
        )

    result = _run_db(scenario)

    assert result == {
        "error": "precondition_failed",
        "reason": "Missing required field(s): brand_name, amount.",
        "required_action": "Ask the user only for the missing input(s): brand_name, amount.",
        "retryable": False,
        "missing": ["brand_name", "amount"],
    }


def test_tool_spec_blocks_checkout_with_empty_cart():
    async def scenario(db):
        return await adapter._validate_tool_preconditions(
            "checkout_cart",
            {},
            db,
            "919111111111",
        )

    result = _run_db(scenario)

    assert result == {
        "error": "precondition_failed",
        "reason": "cart is empty.",
        "required_action": "Ask the user to add items before checkout.",
        "retryable": False,
        "prerequisite_tool": "add_to_cart",
    }


def test_tool_spec_surfaces_invalid_denomination_before_order_mutation(monkeypatch):
    async def fake_find_brand_id(_brand_name, _db=None):
        return "brand_swiggy"

    async def fake_check_brand_amount(_brand_id, _brand_name, _amount):
        return {
            "error": "invalid_denomination",
            "message": "Swiggy supports fixed denominations: ₹500, ₹1000.",
            "available_denominations": [500, 1000],
        }

    monkeypatch.setattr(functions, "_find_brand_id", fake_find_brand_id)
    monkeypatch.setattr(functions, "_check_brand_amount", fake_check_brand_amount)

    async def scenario(db):
        return await adapter._validate_tool_preconditions(
            "create_gift_order",
            {"brand_name": "Swiggy", "amount": 750},
            db,
            "919111111111",
        )

    result = _run_db(scenario)

    assert result["error"] == "invalid_input"
    assert result["reason"] == "Swiggy supports fixed denominations: ₹500, ₹1000."
    assert result["available_denominations"] == [500, 1000]
    assert result["retryable"] is False


def test_same_friend_multiple_due_events_are_rate_limited_per_day(monkeypatch):
    async def fake_send_template(phone, *, template_name, body_params, **_kwargs):
        return {"messages": [{"id": f"wamid.{phone}.{len(body_params)}"}]}

    monkeypatch.setattr(send_reminders.whatsapp_service, "send_template", fake_send_template)

    async def scenario(db):
        first, _, _ = await _seed_celebrant_event(
            db,
            celebrant_id="usr_one",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_one",
            cel_id="cel_one",
            occasion_type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            preferences=["Books"],
            friend_phone="919999999999",
            friend_name="Asha",
        )
        second, _, _ = await _seed_celebrant_event(
            db,
            celebrant_id="usr_two",
            celebrant_phone="918888888888",
            celebrant_name="Niharika",
            occasion_id="occ_two",
            cel_id="cel_two",
            occasion_type=OccasionType.ANNIVERSARY,
            date=datetime(2026, 6, 11, 9, tzinfo=timezone.utc),
            preferences=["Dining"],
            friend_phone="919999999999",
            friend_name="Asha",
        )
        rem_one = Reminder(
            id="rem_one",
            occasion_id=first.id,
            scheduled_at=datetime.now(timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="early_reminder",
            target_phone="919999999999",
        )
        rem_two = Reminder(
            id="rem_two",
            occasion_id=second.id,
            scheduled_at=datetime.now(timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="early_reminder",
            target_phone="919999999999",
        )
        db.add_all([rem_one, rem_two])
        await db.flush()

        first_result = await send_reminders.dispatch_reminder(db, rem_one)
        second_result = await send_reminders.dispatch_reminder(db, rem_two)
        await db.commit()
        return first_result, second_result, rem_one, rem_two

    first_result, second_result, rem_one, rem_two = _run_db(scenario)

    assert first_result["success"] is True
    assert rem_one.status == ReminderStatus.SENT
    assert second_result == {
        "success": True,
        "skipped": True,
        "reminder_id": "rem_two",
        "sent_to": "919999999999",
        "message_type": "early_reminder",
        "reason": "rate_limited: friend already received a reminder in last 24h",
    }
    assert rem_two.status == ReminderStatus.CANCELLED
    assert rem_two.last_error == "rate_limited: friend already received a reminder in last 24h"


def test_external_purchase_blocks_duplicate_friend_order_until_acknowledged(monkeypatch):
    async def fake_find_brand_id(_brand_name, _db=None):
        return "hubble_amazon"

    async def fake_payment_link(**kwargs):
        return {"id": "plink_friend", "short_url": "https://rzp.io/friend"}

    monkeypatch.setattr(order_tools, "_find_brand_id", fake_find_brand_id)
    monkeypatch.setattr(functions.razorpay_service, "create_payment_link", fake_payment_link)

    async def scenario(db):
        occasion, cel_occ, _ = await _seed_celebrant_event(
            db,
            celebrant_id="usr_celebrant",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_birthday",
            cel_id="cel_birthday",
            occasion_type=OccasionType.BIRTHDAY,
            date=datetime(2026, 6, 10, 9, tzinfo=timezone.utc),
            preferences=["Gift Cards"],
            friend_phone="919222222222",
            friend_name="Asha",
        )
        db.add(CelebrantFriend(
            id="cfr_second_friend",
            celebrant_occasion_id=cel_occ.id,
            phone="919333333333",
            name="Rohit",
        ))
        await db.flush()

        logged = await functions.log_external_purchase(db, "919222222222", {
            "occasion_id": occasion.id,
            "item_description": "Amazon gift card",
            "category": "gift_card",
            "brand_name": "Amazon",
        })
        checklist = await functions.check_occasion_gifts(db, "919333333333", {
            "occasion_id": occasion.id,
        })
        blocked = await functions.create_friend_gift_order(db, "919333333333", {
            "celebrant_occasion_id": cel_occ.id,
            "brand_name": "Amazon",
            "amount": 1000,
            "buyer_name": "Rohit",
        })
        acknowledged = await functions.create_friend_gift_order(db, "919333333333", {
            "celebrant_occasion_id": cel_occ.id,
            "brand_name": "Amazon",
            "amount": 1000,
            "buyer_name": "Rohit",
            "duplicate_acknowledged": True,
        })
        await db.commit()
        return logged, checklist, blocked, acknowledged

    logged, checklist, blocked, acknowledged = _run_db(scenario)

    assert logged["success"] is True
    assert checklist["brands_taken"] == ["amazon"]
    assert checklist["categories_taken"] == ["gift_card"]
    assert blocked["duplicate_warning"] is True
    assert "payment_link" not in blocked
    assert acknowledged["success"] is True
    assert acknowledged["payment_link"] == "https://rzp.io/friend"


def test_self_gift_card_purchase_creates_unattached_immediate_order(monkeypatch):
    async def fake_find_brand_id(_brand_name, _db=None):
        return "hubble_myntra"

    async def fake_payment_link(**kwargs):
        return {"id": "plink_self", "short_url": "https://rzp.io/self"}

    monkeypatch.setattr(order_tools, "_find_brand_id", fake_find_brand_id)
    monkeypatch.setattr(functions.razorpay_service, "create_payment_link", fake_payment_link)

    async def scenario(db):
        user = User(id="usr_self", phone="919444444444", name="Self Buyer")
        db.add(user)
        await db.flush()

        result = await functions.create_gift_order(db, user.phone, {
            "brand_name": "Myntra",
            "amount": 500,
        })
        order = await db.get(Order, result["order_id"])
        purchase = (
            await db.execute(select(Purchase).where(Purchase.order_id == order.id))
        ).scalar_one()
        await db.commit()
        return result, order, purchase

    result, order, purchase = _run_db(scenario)

    assert result["success"] is True
    assert result["payment_link"] == "https://rzp.io/self"
    assert result["provider"] == "hubble"
    assert result["provider_product_id"] == "hubble_myntra"
    assert order.user_id == "usr_self"
    assert purchase.occasion_id is None
    assert purchase.delivery_scheduled_at is None
    assert purchase.provider == "hubble"
    assert purchase.provider_product_id == "hubble_myntra"
    assert purchase.fulfillment_policy == FulfillmentPolicy.IMMEDIATE_AFTER_PAYMENT.value
    assert purchase.recipient_user_id == "usr_self"
    assert purchase.recipient_phone == "919444444444"
    assert purchase.status == PurchaseStatus.PENDING
    assert Decimal(purchase.amount) == Decimal("500.00")


def test_select_gift_candidate_persists_marketplace_selection(monkeypatch):
    from app.bot.sources.base import GiftCandidate, ProviderPolicy
    from app.bot.state_utils import get_active_gift_context, get_state
    from app.bot.tools.tool_impl import marketplace_checkout

    candidate = GiftCandidate(
        source_id="vendor_db",
        item_id="ven_cake",
        name="Pune Cake Studio",
        vibes=["bakery_cake"],
        price_min=1200,
        price_max=2500,
        fulfillment="physical_delivery",
        policy=ProviderPolicy(fulfillment_mode="manual"),
    )

    async def fake_resolve(_db, _provider_id, _candidate_id):
        return candidate

    monkeypatch.setattr(marketplace_checkout, "_resolve_marketplace_candidate", fake_resolve)

    async def scenario(db):
        user = User(id="usr_select_candidate", phone="919555555555", name="Karan")
        db.add(user)
        await db.flush()
        result = await functions.select_gift_candidate(
            db,
            user.phone,
            {"provider_id": "vendor_db", "candidate_id": "ven_cake"},
        )
        ctx = get_active_gift_context(await get_state(db, user.phone))
        await db.commit()
        return result, ctx

    result, ctx = _run_db(scenario)

    assert result["success"] is True
    assert result["provider_id"] == "vendor_db"
    assert ctx["selected_provider_id"] == "vendor_db"
    assert ctx["selected_candidate_id"] == "ven_cake"
    assert ctx["selected_brand"] == "Pune Cake Studio"


def test_create_checkout_manual_vendor_creates_manual_review_purchase(monkeypatch):
    from app.bot.sources.base import GiftCandidate, ProviderPolicy
    from app.bot.tools.tool_impl import marketplace_checkout

    candidate = GiftCandidate(
        source_id="vendor_db",
        item_id="ven_flowers",
        name="Pune Fresh Flowers",
        vibes=["fresh_flowers_local"],
        price_min=800,
        price_max=1800,
        fulfillment="physical_delivery",
        policy=ProviderPolicy(
            fulfillment_mode="manual",
            delivery_policy="Same-day in Pune before 5pm",
        ),
        contact_info="ops@example.com",
        metadata={"category": "flowers"},
    )

    async def fake_resolve(_db, _provider_id, _candidate_id):
        return candidate

    monkeypatch.setattr(marketplace_checkout, "_resolve_marketplace_candidate", fake_resolve)

    async def scenario(db):
        user = User(id="usr_manual_checkout", phone="919666666666", name="Karan")
        db.add(user)
        await db.flush()
        result = await functions.create_checkout(
            db,
            user.phone,
            {
                "provider_id": "vendor_db",
                "candidate_id": "ven_flowers",
                "amount": 1200,
                "special_instructions": "red roses",
            },
        )
        order = await db.get(Order, result["order_id"])
        purchase = await db.get(Purchase, result["purchase_id"])
        await db.commit()
        return result, order, purchase

    result, order, purchase = _run_db(scenario)

    assert result["success"] is True
    assert result["manual_review"] is True
    assert order.status == OrderStatus.NEEDS_MANUAL_REVIEW
    assert purchase.provider == "vendor_db"
    assert purchase.provider_product_id == "ven_flowers"
    assert purchase.special_instructions == "red roses"
    assert purchase.fulfillment_metadata_json["contact_info"] == "ops@example.com"


def test_create_checkout_hubble_routes_to_existing_order_flow(monkeypatch):
    from app.bot.sources.base import GiftCandidate, ProviderPolicy
    from app.bot.tools.tool_impl import marketplace_checkout, orders

    candidate = GiftCandidate(
        source_id="hubble",
        item_id="hub_amazon",
        name="Amazon",
        vibes=["mass_electronics"],
        price_min=100,
        price_max=10000,
        fulfillment="instant_voucher",
        policy=ProviderPolicy(fulfillment_mode="api"),
    )

    async def fake_resolve(_db, _provider_id, _candidate_id):
        return candidate

    async def fake_create_gift_order(_db, _phone, args):
        return {
            "success": True,
            "order_id": "ord_fake",
            "brand": args["brand_name"],
            "gift_amount": args["amount"],
            "payment_link": "https://rzp.io/fake",
        }

    monkeypatch.setattr(marketplace_checkout, "_resolve_marketplace_candidate", fake_resolve)
    monkeypatch.setattr(orders, "create_gift_order", fake_create_gift_order)

    async def scenario(db):
        user = User(id="usr_hubble_checkout", phone="919777777777", name="Karan")
        db.add(user)
        await db.flush()
        return await functions.create_checkout(
            db,
            user.phone,
            {"provider_id": "hubble", "candidate_id": "hub_amazon", "amount": 1000},
        )

    result = _run_db(scenario)

    assert result["success"] is True
    assert result["brand"] == "Amazon"
    assert result["payment_link"] == "https://rzp.io/fake"


def test_fulfillment_defaults_support_future_non_gift_card_merchants():
    async def scenario(db):
        buyer = User(id="usr_buyer", phone="919101010101", name="Buyer")
        recipient = User(id="usr_recipient", phone="919202020202", name="Recipient")
        db.add_all([buyer, recipient])
        order = Order(id="ord_cake", user_id=buyer.id, total_amount=1200, status=OrderStatus.PENDING)
        db.add(order)
        await db.flush()

        delivery_at = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
        purchase = Purchase(
            id="pur_cake",
            order_id=order.id,
            occasion_id=None,
            gift_type=GiftType.CAKE,
            brand_name="Local Bakery",
            amount=1200,
            status=PurchaseStatus.PENDING,
            delivery_scheduled_at=delivery_at,
            delivery_status=DeliveryStatus.PENDING_DELIVERY,
        )
        apply_fulfillment_defaults(
            purchase,
            provider_product_id="cake_black_forest",
            recipient_user_id=recipient.id,
            recipient_phone=recipient.phone,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        db.add(purchase)
        await db.flush()
        return purchase

    purchase = _run_db(scenario)

    assert purchase.provider == "local_vendor"
    assert purchase.provider_product_id == "cake_black_forest"
    assert purchase.recipient_user_id == "usr_recipient"
    assert purchase.recipient_phone == "919202020202"
    assert purchase.fulfillment_policy == FulfillmentPolicy.HOURS_BEFORE.value
    assert purchase.fulfillment_lead_time_hours == 24
    assert purchase.fulfillment_due_at == datetime(2026, 6, 9, 12, tzinfo=timezone.utc)


def test_private_theatre_defaults_to_immediate_booking():
    due = compute_fulfillment_due_at(
        policy=FulfillmentPolicy.IMMEDIATE_AFTER_PAYMENT,
        delivery_scheduled_at=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
        now=datetime(2026, 6, 1, 9, tzinfo=timezone.utc),
    )

    assert due == datetime(2026, 6, 1, 9, tzinfo=timezone.utc)


def test_scheduled_voucher_job_only_picks_hubble_gift_cards():
    src = inspect.getsource(send_reminders)
    assert "GiftType.GIFT_CARD" not in src  # reminder job is channel-only, not fulfillment

    from app.jobs import deliver_gifts

    deliver_src = inspect.getsource(deliver_gifts.deliver_scheduled_gifts)
    process_src = inspect.getsource(deliver_gifts._process_due_purchase)
    assert "Purchase.fulfillment_due_at <= now" in deliver_src
    assert "FulfillmentPolicy.MANUAL_REVIEW.value" in deliver_src
    assert "Purchase.gift_type == GiftType.GIFT_CARD" not in deliver_src
    assert "provider == \"hubble\"" in inspect.getsource(deliver_gifts._can_auto_fulfill_with_hubble)
    assert "await _mark_manual_review" in process_src
    assert "asyncio.gather" in deliver_src
    assert "with_for_update" in process_src and "skip_locked" in process_src


def test_paid_webhook_retries_resume_after_claim_before_voucher(monkeypatch):
    """A Razorpay retry must repair a claimed order that never reached Hubble."""
    from app.webhooks import razorpay_handlers

    async def fake_issue_hubble_voucher(purchase):
        purchase.hubble_order_id = "hub_resume"
        purchase.provider = "hubble"
        purchase.provider_product_id = purchase.hubble_product_id
        purchase.provider_order_id = "hub_resume"
        purchase.voucher_code = "CODE123"
        purchase.voucher_pin = "9999"
        purchase.status = PurchaseStatus.VOUCHER_ISSUED
        return {"id": "hub_resume", "vouchers": [{"cardNumber": "CODE123", "cardPin": "9999"}]}

    async def fake_send_text(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(razorpay_handlers, "issue_hubble_voucher", fake_issue_hubble_voucher)
    monkeypatch.setattr(razorpay_handlers.settings, "mvp_reminder_only", False)
    monkeypatch.setattr(razorpay_handlers, "is_in_cs_window", lambda last_inbound_at: True)
    monkeypatch.setattr(razorpay_handlers.whatsapp_service, "send_text", fake_send_text)

    async def scenario(db):
        user = User(
            id="usr_resume",
            phone="919000000001",
            name="Karan",
            last_inbound_at=datetime.now(timezone.utc),
        )
        order = Order(
            id="ord_resume",
            user_id=user.id,
            total_amount=Decimal("1000"),
            status=OrderStatus.PROCESSING,
            razorpay_payment_id="pay_resume",
        )
        purchase = Purchase(
            id="pur_resume",
            order_id=order.id,
            occasion_id=None,
            gift_type=GiftType.GIFT_CARD,
            brand_name="Amazon",
            hubble_product_id="hubble_amazon",
            amount=Decimal("1000"),
            status=PurchaseStatus.PENDING,
        )
        db.add_all([user, order, purchase])
        await db.commit()

        payload = {
            "payload": {
                "payment_link": {"entity": {"reference_id": order.id}},
                "payment": {"entity": {"id": "pay_resume"}},
            }
        }

        result = await razorpay_handlers.handle_payment_link_paid(payload, db)
        refreshed_order = await db.get(Order, order.id)
        refreshed_purchase = await db.get(Purchase, purchase.id)

        assert result["status"] == "ok"
        assert refreshed_order.status == OrderStatus.COMPLETED
        assert refreshed_purchase.hubble_order_id == "hub_resume"
        assert refreshed_purchase.status == PurchaseStatus.DELIVERED

    _run_db(scenario)


def test_paid_webhook_ignores_fulfillment_in_mvp(monkeypatch):
    """MVP reminder-only must not issue Hubble vouchers from paid webhooks."""
    from app.webhooks import razorpay_handlers

    issued: list[str] = []
    alerts: list[str] = []

    async def fake_issue_hubble_voucher(purchase):
        issued.append(purchase.id)
        return {}

    async def fake_alert(message):
        alerts.append(message)

    monkeypatch.setattr(razorpay_handlers.settings, "mvp_reminder_only", True)
    monkeypatch.setattr(razorpay_handlers, "issue_hubble_voucher", fake_issue_hubble_voucher)
    monkeypatch.setattr(razorpay_handlers, "send_founder_alert", fake_alert)

    async def scenario(db):
        user = User(id="usr_mvp_paid", phone="919000000002", name="Karan")
        order = Order(
            id="ord_mvp_paid",
            user_id=user.id,
            total_amount=Decimal("1000"),
            status=OrderStatus.PENDING,
        )
        purchase = Purchase(
            id="pur_mvp_paid",
            order_id=order.id,
            occasion_id=None,
            gift_type=GiftType.GIFT_CARD,
            brand_name="Amazon",
            hubble_product_id="hubble_amazon",
            amount=Decimal("1000"),
            status=PurchaseStatus.PENDING,
        )
        db.add_all([user, order, purchase])
        await db.commit()

        payload = {
            "payload": {
                "payment_link": {"entity": {"reference_id": order.id}},
                "payment": {"entity": {"id": "pay_mvp_paid"}},
            }
        }

        result = await razorpay_handlers.handle_payment_link_paid(payload, db)
        refreshed_order = await db.get(Order, order.id)
        refreshed_purchase = await db.get(Purchase, purchase.id)

        assert result == {
            "status": "ignored_mvp_reminder_only",
            "order_id": order.id,
            "payment_id": "pay_mvp_paid",
        }
        assert refreshed_order.status == OrderStatus.PENDING
        assert refreshed_order.razorpay_payment_id is None
        assert refreshed_purchase.status == PurchaseStatus.PENDING
        assert refreshed_purchase.hubble_order_id is None

    _run_db(scenario)
    assert issued == []
    assert alerts and "MVP_REMINDER_ONLY=true" in alerts[0]


def test_cart_ttl_pure_logic():
    """_cart_expired uses CART_TTL_HOURS correctly — no hardcoded magic number."""
    from datetime import timedelta
    from app.bot.tools.functions import _cart_expired, CART_TTL_HOURS

    now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
    item = [{"brand_name": "Amazon", "amount": 500}]

    # Empty cart is never considered expired.
    assert _cart_expired({}, now=now) is False
    assert _cart_expired({CART_KEY: []}, now=now) is False

    # Cart created 1 hour ago is fresh.
    fresh_ts = (now - timedelta(hours=1)).isoformat()
    assert _cart_expired({CART_KEY: item, CART_CREATED_AT_KEY: fresh_ts}, now=now) is False

    # Cart created exactly at the TTL boundary (CART_TTL_HOURS + 1) is expired.
    stale_ts = (now - timedelta(hours=CART_TTL_HOURS + 1)).isoformat()
    assert _cart_expired({CART_KEY: item, CART_CREATED_AT_KEY: stale_ts}, now=now) is True

    # Cart with items but no timestamp is treated as expired (defensive).
    assert _cart_expired({CART_KEY: item}, now=now) is True

    # Garbage timestamp is treated as expired.
    assert _cart_expired({CART_KEY: item, CART_CREATED_AT_KEY: "not-a-date"}, now=now) is True


def test_load_active_cart_clears_expired_state():
    """_load_active_cart must wipe an expired cart from DB state and signal expired=True."""
    from datetime import timedelta
    from app.bot.tools.functions import _load_active_cart, CART_TTL_HOURS

    async def scenario(db):
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=CART_TTL_HOURS + 1)).isoformat()

        user = User(id="usr_cart_exp", phone="919100000099", name="Expired Cart User")
        db.add(user)
        state = ConversationState(
            phone=user.phone,
            temp_data=json.dumps({
                CART_KEY: [{"brand_name": "Flipkart", "amount": 300}],
                CART_CREATED_AT_KEY: stale_ts,
            }),
        )
        db.add(state)
        await db.flush()

        cart, temp, _, expired = await _load_active_cart(db, user.phone)
        return cart, temp, expired

    cart, temp, expired = _run_db(scenario)

    assert cart == [], "expired cart must return empty list"
    assert expired is True, "_load_active_cart must signal expiry"
    assert temp.get(CART_KEY) == [], "state must be wiped after expiry"
    assert CART_CREATED_AT_KEY not in temp, "timestamp must be removed after expiry"


def test_process_due_purchase_skips_locked_row(monkeypatch):
    """When SELECT FOR UPDATE SKIP LOCKED returns no row, _process_due_purchase exits silently."""
    from unittest.mock import AsyncMock, MagicMock
    from app.jobs.deliver_gifts import _process_due_purchase

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # simulates locked row skipped

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr("app.jobs.deliver_gifts.async_session", MagicMock(return_value=mock_db))

    asyncio.run(_process_due_purchase("pur_locked_by_other_worker"))

    # Nothing committed — the row was skipped entirely.
    mock_db.commit.assert_not_called()


def test_deleting_occasion_with_friend_links_soft_deletes_and_keeps_context():
    """Incident 2026-07-17: a friend clicked a reminder link whose celebrant
    occasion had been hard-deleted (FK cascade), so get_celebrant_context died
    and the bot looped on "re-open the link". Deleting an occasion that still
    has invited friends must soft-delete it: hidden from the owner, but the
    friend link keeps resolving."""

    async def scenario(db):
        occasion, cel_occ, _friend = await _seed_celebrant_event(
            db,
            celebrant_id="usr_karan",
            celebrant_phone="919111111111",
            celebrant_name="Karan",
            occasion_id="occ_bday",
            cel_id="cel_bday",
            occasion_type=OccasionType.BIRTHDAY,
            date=datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
            + timedelta(days=30),
            preferences=["Books"],
            friend_phone="919222222222",
            friend_name="Alisha",
        )
        await schedule_reminders(db, occasion, target_phones=["919222222222"])
        await db.commit()

        deleted = await functions.delete_occasion(
            db, "919111111111", {"occasion_id": "occ_bday"}
        )
        await db.commit()

        # Owner no longer sees it.
        owner_list = await functions.list_occasions(db, "919111111111", {})

        # Row survives, flagged deleted.
        row = await db.get(Occasion, "occ_bday")

        # Friend link still resolves.
        friend_ctx = await functions.get_celebrant_context(
            db, "919222222222", {"celebrant_occasion_id": "cel_bday"}
        )

        # Reminders were cleared so nobody gets nudged for a deleted occasion.
        remaining = (
            await db.execute(select(Reminder).where(Reminder.occasion_id == "occ_bday"))
        ).scalars().all()

        return deleted, owner_list, row, friend_ctx, remaining

    deleted, owner_list, row, friend_ctx, remaining = _run_db(scenario)

    assert deleted.get("success") is True
    assert not owner_list.get("occasions"), "soft-deleted occasion must not appear to owner"
    assert row is not None, "row must survive so the friend link resolves"
    assert row.deleted_at is not None, "row must be flagged soft-deleted"
    assert friend_ctx.get("preferences") == ["Books"], "friend context must still resolve"
    assert remaining == [], "pending reminders must be cleared"


def test_deleting_occasion_without_friends_hard_deletes():
    """No friend links → plain hard delete, no soft-delete ghost left behind."""

    async def scenario(db):
        user = User(id="usr_solo", phone="919444444444", name="Solo")
        db.add(user)
        await db.flush()
        occ = Occasion(
            id="occ_solo",
            user_id=user.id,
            contact_id=None,
            type=OccasionType.BIRTHDAY,
            date=datetime.now(timezone.utc) + timedelta(days=20),
            recurring=True,
        )
        db.add(occ)
        await db.commit()

        await functions.delete_occasion(db, "919444444444", {"occasion_id": "occ_solo"})
        await db.commit()
        return await db.get(Occasion, "occ_solo")

    row = _run_db(scenario)
    assert row is None, "occasion without friends must be hard-deleted"
