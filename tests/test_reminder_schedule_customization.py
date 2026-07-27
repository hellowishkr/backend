"""Regressions for user-selected occasion reminder schedules."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl.reminders import set_reminder_schedule
from app.models import Contact, Occasion, OccasionType, Reminder, ReminderStatus, User
from app.services.reminder import schedule_reminders


async def _occasion(
    db: AsyncSession,
    *,
    phone: str,
    occasion_id: str,
    days_from_now: int,
) -> Occasion:
    user = User(id=f"usr_{occasion_id}", phone=phone, name="Disha")
    contact = Contact(
        id=f"con_{occasion_id}",
        user_id=user.id,
        name="Vidushi",
        relationship_type="friend",
    )
    occasion = Occasion(
        id=occasion_id,
        user_id=user.id,
        contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc) + timedelta(days=days_from_now),
        recurring=True,
    )
    db.add_all([user, contact, occasion])
    await db.flush()
    return occasion


@pytest.mark.asyncio
async def test_tight_window_does_not_create_unsolicited_immediate_reminder(
    db: AsyncSession,
) -> None:
    occasion = await _occasion(
        db,
        phone="919000000101",
        occasion_id="occ_tight_window",
        days_from_now=1,
    )

    summary = await schedule_reminders(db, occasion)
    rows = (
        await db.execute(
            select(Reminder).where(Reminder.occasion_id == occasion.id)
        )
    ).scalars().all()

    assert summary["tight_window"] is True
    assert summary["skipped_past"] == 3
    assert [row.message_type for row in rows] == ["day_of_reminder"]
    assert all(row.scheduled_at > datetime.now(timezone.utc).replace(tzinfo=None) for row in rows)


@pytest.mark.asyncio
async def test_set_reminder_schedule_replaces_only_owner_pending_rows(
    db: AsyncSession,
) -> None:
    phone = "919000000102"
    occasion = await _occasion(
        db,
        phone=phone,
        occasion_id="occ_vidushi",
        days_from_now=3,
    )
    now = datetime.now(timezone.utc)
    db.add_all([
        Reminder(
            id="rem_old_immediate",
            occasion_id=occasion.id,
            scheduled_at=now,
            status=ReminderStatus.PENDING,
            message_type="tight_window_reminder",
        ),
        Reminder(
            id="rem_sent_history",
            occasion_id=occasion.id,
            scheduled_at=now - timedelta(days=1),
            status=ReminderStatus.SENT,
            message_type="early_reminder",
        ),
        Reminder(
            id="rem_friend_target",
            occasion_id=occasion.id,
            scheduled_at=now + timedelta(days=1),
            status=ReminderStatus.PENDING,
            message_type="early_reminder",
            target_phone="919111111111",
        ),
    ])
    await db.flush()

    result = await set_reminder_schedule(
        db,
        phone,
        {"occasion_id": occasion.id, "days_before": [1, 0]},
    )
    rows = (
        await db.execute(
            select(Reminder)
            .where(Reminder.occasion_id == occasion.id)
            .order_by(Reminder.scheduled_at)
        )
    ).scalars().all()

    owner_pending = [
        row for row in rows
        if row.status == ReminderStatus.PENDING and row.target_phone is None
    ]
    assert result["success"] is True
    assert result["days_before"] == [1, 0]
    assert result["cancelled_pending"] == 1
    assert {row.message_type for row in owner_pending} == {
        "one_day_before_reminder",
        "day_of_reminder",
    }
    assert {row.id for row in rows}.issuperset({"rem_sent_history", "rem_friend_target"})
    assert "rem_old_immediate" not in {row.id for row in rows}


@pytest.mark.asyncio
async def test_set_reminder_schedule_rejects_invalid_offsets_without_changes(
    db: AsyncSession,
) -> None:
    phone = "919000000103"
    occasion = await _occasion(
        db,
        phone=phone,
        occasion_id="occ_invalid_schedule",
        days_from_now=5,
    )
    existing = Reminder(
        id="rem_existing",
        occasion_id=occasion.id,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=2),
        status=ReminderStatus.PENDING,
        message_type="last_minute_reminder",
    )
    db.add(existing)
    await db.flush()

    result = await set_reminder_schedule(
        db,
        phone,
        {"occasion_id": occasion.id, "days_before": [-1, 0]},
    )

    assert result["error"] == "invalid_days_before"
    assert await db.get(Reminder, existing.id) is existing


@pytest.mark.asyncio
async def test_set_reminder_schedule_uses_next_recurring_occurrence(
    db: AsyncSession,
) -> None:
    phone = "919000000104"
    occasion = await _occasion(
        db,
        phone=phone,
        occasion_id="occ_recurring",
        days_from_now=-30,
    )

    result = await set_reminder_schedule(
        db,
        phone,
        {"occasion_id": occasion.id, "days_before": [1, 0]},
    )

    assert result["success"] is True
    assert all(
        datetime.fromisoformat(row["scheduled_at"]) > datetime.now(timezone.utc)
        for row in result["scheduled_reminders"]
    )
