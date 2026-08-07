"""`delete_all_occasions` — the tool that did not exist.

2026-08 transcript: the user asked "Can you delete all the reminders". No bulk
delete tool existed, so the agent improvised — it listed the occasions, asked
for confirmation, and then replied "All your saved occasions and their
reminders have been deleted" with nothing verifying that claim.

These tests pin the two properties that matter for a destructive action:
confirmation is required, and the reported count is real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl.occasions import delete_all_occasions
from app.models import (
    CelebrantFriend,
    CelebrantOccasion,
    Contact,
    Occasion,
    OccasionType,
    Reminder,
    ReminderStatus,
    User,
)
from app.utils.id_gen import generate_id


async def _user_with_occasions(db: AsyncSession, phone: str, count: int = 3):
    user = User(id=generate_id("usr"), phone=phone, name="DJ")
    db.add(user)
    await db.flush()
    for i in range(count):
        contact = Contact(
            id=generate_id("con"), user_id=user.id, name=f"Friend{i}",
        )
        db.add(contact)
        await db.flush()
        occ = Occasion(
            id=generate_id("occ"),
            user_id=user.id,
            contact_id=contact.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2026, 8, 10 + i, tzinfo=timezone.utc),
            recurring=True,
        )
        db.add(occ)
        await db.flush()
        db.add(
            Reminder(
                id=generate_id("rem"),
                occasion_id=occ.id,
                target_phone=phone,
                message_type="pre_reminder",
                scheduled_at=datetime(2026, 8, 9 + i, tzinfo=timezone.utc),
                status=ReminderStatus.PENDING,
            )
        )
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_unconfirmed_call_deletes_nothing_and_asks_first(db: AsyncSession):
    """The exact bug: a bulk delete must never happen on the first call."""
    phone = "919000000101"
    user = await _user_with_occasions(db, phone)

    result = await delete_all_occasions(db, phone, {})

    assert result.get("confirmation_required") is True
    assert result["would_delete_count"] == 3
    assert len(result["would_delete"]) == 3
    assert "success" not in result

    # Nothing was actually removed.
    remaining = (
        await db.execute(select(Occasion).where(Occasion.user_id == user.id))
    ).scalars().all()
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_confirmed_call_deletes_all_and_reports_a_real_count(db: AsyncSession):
    phone = "919000000102"
    user = await _user_with_occasions(db, phone)

    result = await delete_all_occasions(db, phone, {"confirmed": True})

    assert result["success"] is True
    assert result["deleted_count"] == 3
    assert result["cancelled_reminders"] == 3

    remaining = (
        await db.execute(
            select(Occasion).where(
                Occasion.user_id == user.id, Occasion.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    assert remaining == []

    reminders = (await db.execute(select(Reminder))).scalars().all()
    assert reminders == []


@pytest.mark.asyncio
async def test_no_occasions_reports_zero_not_a_false_confirmation(db: AsyncSession):
    phone = "919000000103"
    db.add(User(id=generate_id("usr"), phone=phone, name="DJ"))
    await db.flush()

    result = await delete_all_occasions(db, phone, {"confirmed": True})

    assert result["deleted_count"] == 0
    assert "no saved occasions" in result["message"].lower()


@pytest.mark.asyncio
async def test_occasion_with_invited_friends_is_soft_deleted(db: AsyncSession):
    """Friends already hold reminder links — those must keep resolving.

    Same rule as `delete_occasion`: hide it from the owner, keep the row.
    """
    phone = "919000000104"
    user = await _user_with_occasions(db, phone, count=1)
    occ = (
        await db.execute(select(Occasion).where(Occasion.user_id == user.id))
    ).scalars().one()

    cel = CelebrantOccasion(id=generate_id("celocc"), occasion_id=occ.id)
    db.add(cel)
    await db.flush()
    db.add(
        CelebrantFriend(
            id=generate_id("celfr"),
            celebrant_occasion_id=cel.id,
            phone="919000000999",
            name="Invited",
        )
    )
    await db.flush()

    result = await delete_all_occasions(db, phone, {"confirmed": True})
    assert result["deleted_count"] == 1

    # Row survives, but is hidden from the owner.
    still_there = await db.get(Occasion, occ.id)
    assert still_there is not None
    assert still_there.deleted_at is not None
