from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl import celebrant, occasions, wishlist
from app.core.config import settings
from app.jobs import send_monthly_summary, send_reminders, wishlist_lifecycle
from app.models import (
    Contact,
    Occasion,
    OccasionType,
    Reminder,
    ReminderStatus,
    User,
    WishlistItem,
)
from app.services.reminder import schedule_reminders
from app.utils.dates import next_occurrence
from app.utils.timezones import (
    DEFAULT_TIMEZONE_NAME,
    local_month_bounds_utc,
    local_schedule_utc,
    today_for_user,
)


def test_user_timezone_helpers_use_saved_iana_zone() -> None:
    user = User(
        id="usr_timezone_unit",
        phone="919000000820",
        name="Disha",
        timezone="America/New_York",
    )
    now = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    occasion = datetime(2026, 7, 27, tzinfo=timezone.utc)

    assert today_for_user(user, now) == datetime(2026, 7, 26).date()
    assert local_schedule_utc(occasion, user) == datetime(
        2026,
        7,
        27,
        13,
        tzinfo=timezone.utc,
    )


def test_next_occurrence_uses_explicit_local_reference_date() -> None:
    occasion = Occasion(
        id="occ_timezone_rollover",
        user_id="usr_timezone_rollover",
        type=OccasionType.BIRTHDAY,
        date=datetime(2025, 12, 31, tzinfo=timezone.utc),
        recurring=True,
    )

    result = next_occurrence(
        occasion,
        datetime(2027, 1, 1, 1, tzinfo=timezone.utc),
        reference_date=date(2026, 12, 31),
    )

    assert result.date() == date(2026, 12, 31)


def test_local_month_bounds_follow_user_timezone() -> None:
    user = User(
        id="usr_timezone_month",
        phone="919000000830",
        timezone="America/Los_Angeles",
    )

    month_start, next_month_start = local_month_bounds_utc(
        user,
        datetime(2026, 8, 1, 2, tzinfo=timezone.utc),
    )

    # It is still July 31 for this user, so the current local month is July.
    assert month_start == datetime(2026, 7, 1, 7, tzinfo=timezone.utc)
    assert next_month_start == datetime(2026, 8, 1, 7, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_occasion_creation_uses_user_local_year(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "919000000822"
    db.add(
        User(
            id="usr_timezone_create",
            phone=phone,
            name="Disha",
            timezone="Pacific/Pago_Pago",
        )
    )
    await db.flush()
    local_today = date(2030, 12, 31)
    monkeypatch.setattr(
        occasions,
        "today_for_user",
        lambda user, now=None: local_today,
    )
    monkeypatch.setattr(
        celebrant,
        "today_for_user",
        lambda user, now=None: local_today,
    )

    contact_result = await occasions.add_occasion(
        db,
        phone,
        {
            "contact_name": "Swaraj",
            "occasion_type": "BIRTHDAY",
            "date_day": 1,
            "date_month": 1,
        },
    )
    own_result = await celebrant.setup_celebrant_occasion(
        db,
        phone,
        {
            "occasion_type": "BIRTHDAY",
            "date_day": 1,
            "date_month": 1,
        },
    )

    contact_occasion = await db.get(Occasion, contact_result["occasion_id"])
    own_occasion = await db.get(Occasion, own_result["occasion_id"])
    assert contact_occasion is not None
    assert own_occasion is not None
    assert contact_occasion.date.date() == date(2031, 1, 1)
    assert own_occasion.date.date() == date(2031, 1, 1)


@pytest.mark.asyncio
async def test_default_reminders_use_owner_saved_timezone(
    db: AsyncSession,
) -> None:
    user = User(
        id="usr_timezone_schedule",
        phone="919000000821",
        name="Disha",
        timezone="America/New_York",
    )
    occasion = Occasion(
        id="occ_timezone_schedule",
        user_id=user.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2027, 7, 27, tzinfo=timezone.utc),
        recurring=True,
    )
    db.add_all([user, occasion])
    await db.flush()

    await schedule_reminders(db, occasion)

    reminders = (
        await db.execute(
            select(Reminder)
            .where(Reminder.occasion_id == occasion.id)
            .order_by(Reminder.scheduled_at)
        )
    ).scalars().all()
    assert reminders
    assert all(reminder.scheduled_at.hour == 13 for reminder in reminders)


def test_recipient_window_uses_the_actual_message_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "reminder_recipient_window_start_hour", 6)
    monkeypatch.setattr(settings, "reminder_recipient_window_start_minute", 30)
    monkeypatch.setattr(settings, "reminder_recipient_window_end_hour", 22)
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)

    owner_open = send_reminders._recipient_window_is_open(
        now,
        "Asia/Kolkata",
    )
    friend_open = send_reminders._recipient_window_is_open(
        now,
        "America/Los_Angeles",
    )

    assert owner_open is True
    assert friend_open is False
    assert send_reminders._recipient_window_is_open(
        now,
        DEFAULT_TIMEZONE_NAME,
    ) is True


@pytest.mark.asyncio
async def test_due_batch_excludes_closed_timezone_without_starving_open_recipient(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "reminder_batch_size", 1)
    monkeypatch.setattr(settings, "reminder_recipient_window_start_hour", 6)
    monkeypatch.setattr(settings, "reminder_recipient_window_start_minute", 30)
    monkeypatch.setattr(settings, "reminder_recipient_window_end_hour", 22)
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)

    owner = User(
        id="usr_timezone_batch_owner",
        phone="919000000825",
        name="Disha",
        timezone="Asia/Kolkata",
    )
    friend = User(
        id="usr_timezone_batch_friend",
        phone="919000000826",
        name="Asha",
        timezone="America/Los_Angeles",
    )
    occasion = Occasion(
        id="occ_timezone_batch",
        user_id=owner.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2027, 7, 27, tzinfo=timezone.utc),
        recurring=True,
    )
    closed_friend_reminder = Reminder(
        id="rem_timezone_closed_friend",
        occasion_id=occasion.id,
        scheduled_at=now - timedelta(hours=2),
        status=ReminderStatus.PENDING,
        message_type="early_reminder",
        target_phone=friend.phone,
    )
    open_owner_reminder = Reminder(
        id="rem_timezone_open_owner",
        occasion_id=occasion.id,
        scheduled_at=now - timedelta(hours=1),
        status=ReminderStatus.PENDING,
        message_type="early_reminder",
        target_phone=None,
    )
    db.add_all([
        owner,
        friend,
        occasion,
        closed_friend_reminder,
        open_owner_reminder,
    ])
    await db.flush()

    reminder_ids = await send_reminders._eligible_due_reminder_ids(db, now)

    assert reminder_ids == [open_owner_reminder.id]
    assert closed_friend_reminder.status == ReminderStatus.PENDING
    assert closed_friend_reminder.scheduled_at == now - timedelta(hours=2)


@pytest.mark.asyncio
async def test_dispatch_recheck_skips_before_claiming(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = User(
        id="usr_timezone_recheck",
        phone="919000000827",
        name="Disha",
        timezone="America/Los_Angeles",
    )
    occasion = Occasion(
        id="occ_timezone_recheck",
        user_id=owner.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2027, 7, 27, tzinfo=timezone.utc),
        recurring=True,
    )
    reminder = Reminder(
        id="rem_timezone_recheck",
        occasion_id=occasion.id,
        scheduled_at=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        status=ReminderStatus.PENDING,
        message_type="early_reminder",
        target_phone=None,
    )
    db.add_all([owner, occasion, reminder])
    await db.flush()

    original_schedule = reminder.scheduled_at

    @asynccontextmanager
    async def _test_session():
        yield db

    monkeypatch.setattr(send_reminders, "async_session", _test_session)
    monkeypatch.setattr(
        send_reminders,
        "_recipient_window_is_open",
        lambda now, timezone_name: False,
    )
    with patch(
        "app.jobs.send_reminders.dispatch_reminder",
        new=AsyncMock(),
    ) as dispatch:
        sent = await send_reminders._dispatch_one(
            reminder.id,
            send_reminders.asyncio.Semaphore(1),
        )

    assert sent is False
    assert reminder.status == ReminderStatus.PENDING
    assert reminder.scheduled_at == original_schedule
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_monthly_summary_uses_user_local_month_and_recurring_date(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_user = User(
        id="usr_timezone_monthly_local",
        phone="919000000828",
        name="Disha",
        timezone="America/New_York",
    )
    closed_user = User(
        id="usr_timezone_monthly_closed",
        phone="919000000829",
        name="Karan",
        timezone="America/Los_Angeles",
    )
    contact = Contact(
        id="con_timezone_monthly",
        user_id=local_user.id,
        name="Swaraj",
    )
    db.add_all(
        [
            local_user,
            closed_user,
            contact,
            Occasion(
                id="occ_timezone_monthly_local",
                user_id=local_user.id,
                contact_id=contact.id,
                type=OccasionType.BIRTHDAY,
                date=datetime(1990, 7, 10, tzinfo=timezone.utc),
                recurring=True,
            ),
            Occasion(
                id="occ_timezone_monthly_closed",
                user_id=closed_user.id,
                type=OccasionType.BIRTHDAY,
                date=datetime(1990, 7, 12, tzinfo=timezone.utc),
                recurring=True,
            ),
        ]
    )
    await db.flush()

    @asynccontextmanager
    async def _test_session():
        yield db

    send = AsyncMock()
    monkeypatch.setattr(send_monthly_summary, "async_session", _test_session)
    monkeypatch.setattr(send_monthly_summary, "get_redis", lambda: None)
    monkeypatch.setattr(send_monthly_summary, "send_text_or_template", send)

    # 09:00 on July 1 in New York, but only 06:00 in Los Angeles.
    await send_monthly_summary.send_monthly_occasion_summaries(
        now=datetime(2026, 7, 1, 13, tzinfo=timezone.utc)
    )

    send.assert_awaited_once()
    phone, text = send.await_args.args[:2]
    assert phone == local_user.phone
    assert "July 2026" in text
    assert "Swaraj's Birthday" in text
    assert "10 Jul" in text


@pytest.mark.asyncio
async def test_wishlist_reminder_uses_owner_local_date_before_limit(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_user = User(
        id="usr_timezone_wishlist_local",
        phone="919000000831",
        name="Disha",
        timezone="America/New_York",
    )
    closed_user = User(
        id="usr_timezone_wishlist_closed",
        phone="919000000832",
        name="Karan",
        timezone="America/Los_Angeles",
    )
    local_item = WishlistItem(
        id="wsh_timezone_local",
        user_id=local_user.id,
        url="https://example.com/local",
        title="Local gift",
        remind_on=date(2026, 7, 1),
    )
    closed_item = WishlistItem(
        id="wsh_timezone_closed",
        user_id=closed_user.id,
        url="https://example.com/closed",
        title="Closed gift",
        remind_on=date(2026, 7, 1),
    )
    db.add_all([local_user, closed_user, local_item, closed_item])
    await db.flush()

    @asynccontextmanager
    async def _test_session():
        yield db

    send = AsyncMock()
    monkeypatch.setattr(wishlist_lifecycle, "async_session", _test_session)
    monkeypatch.setattr(wishlist_lifecycle, "send_text_or_template", send)

    # 08:00 in New York, but only 05:00 in Los Angeles.
    now = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    await wishlist_lifecycle.send_wishlist_reminders(now=now)

    send.assert_awaited_once()
    assert send.await_args.args[0] == local_user.phone
    await db.refresh(local_item)
    await db.refresh(closed_item)
    assert local_item.reminded_at is not None
    assert local_item.reminded_at.replace(tzinfo=timezone.utc) == now
    assert closed_item.reminded_at is None


@pytest.mark.asyncio
async def test_wishlist_past_date_validation_uses_user_local_today(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id="usr_timezone_wishlist_validation",
        phone="919000000833",
        name="Disha",
        timezone="Pacific/Pago_Pago",
    )
    db.add(user)
    await db.flush()
    monkeypatch.setattr(
        wishlist,
        "today_for_user",
        lambda saved_user, now=None: date(2030, 1, 2),
    )

    result = await wishlist.add_to_wishlist(
        db,
        user.phone,
        {
            "url": "https://example.com/product",
            "remind_on": "2030-01-01",
        },
    )

    assert result["error"] == "remind_on_in_past"
