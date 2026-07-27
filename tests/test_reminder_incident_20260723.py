"""Regressions from the 2026-07-23 incident.

1. get_upcoming_digest with an empty window must surface the nearest
   occasion beyond it (wedding on day 8 → "no upcoming occasions" dead-end).
2. send_reminder_now must never consume a scheduled PENDING reminder
   (an ad-hoc send on 7 Jul marked the 10-days-before early_reminder SENT,
   so nothing fired on 21 Jul).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl import occasions, reminders as reminders_tool
from app.models import Contact, Occasion, Reminder, ReminderStatus, User
from app.models.occasion import OccasionType


async def _seed_wedding(db: AsyncSession, phone: str, days_out: int) -> Occasion:
    user = User(id="usr_inc", phone=phone, name="Karan", city_raw="Pune")
    contact = Contact(id="con_inc", user_id=user.id, name="Swaraj", relationship_type="friend")
    occ = Occasion(
        id="occ_inc",
        user_id=user.id,
        contact_id=contact.id,
        type=OccasionType.WEDDING,
        date=datetime.now(timezone.utc) + timedelta(days=days_out),
        recurring=False,
    )
    db.add_all([user, contact, occ])
    await db.flush()
    return occ


@pytest.mark.asyncio
async def test_empty_digest_window_surfaces_next_occasion(db: AsyncSession) -> None:
    phone = "919000000010"
    occ = await _seed_wedding(db, phone, days_out=8)  # just beyond 7-day window

    result = await occasions.get_upcoming_digest(db, phone, {"days": 7})
    assert result["total_upcoming"] == 0
    beyond = result.get("next_occasion_beyond_window")
    assert beyond is not None
    assert beyond["occasion_id"] == occ.id
    assert beyond["contact_name"] == "Swaraj"
    assert "hint" in result


@pytest.mark.asyncio
async def test_send_reminder_now_never_consumes_scheduled_reminder(db: AsyncSession) -> None:
    phone = "919000000011"
    occ = await _seed_wedding(db, phone, days_out=24)

    scheduled = Reminder(
        id="rem_inc_early",
        occasion_id=occ.id,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=14),
        status=ReminderStatus.PENDING,
        message_type="early_reminder",
        target_phone=None,
    )
    db.add(scheduled)
    await db.flush()

    with patch(
        "app.jobs.send_reminders.dispatch_reminder",
        new=AsyncMock(return_value={
            "success": True, "sent_to": phone,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "message_type": "ad_hoc_reminder",
        }),
    ) as mock_dispatch:
        result = await reminders_tool.send_reminder_now(
            db, phone, {"occasion_id": occ.id},
        )

    assert result.get("success") is True
    assert result.get("ad_hoc") is True
    # The dispatched row must be a NEW ad-hoc row, not the scheduled one.
    dispatched = mock_dispatch.call_args.args[1]
    assert dispatched.id != scheduled.id
    assert dispatched.message_type == "ad_hoc_reminder"
    # The scheduled early reminder must remain untouched and PENDING.
    row = (
        await db.execute(select(Reminder).where(Reminder.id == scheduled.id))
    ).scalar_one()
    assert row.status == ReminderStatus.PENDING
    assert row.sent_at is None
