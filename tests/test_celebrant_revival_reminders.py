"""Reviving a soft-deleted celebrant occasion must re-create reminder streams
for the retained existing friends — delete_occasion() purged them."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.tools.tool_impl.celebrant import setup_celebrant_occasion
from app.bot.tools.tool_impl.occasions import delete_occasion
from app.db.base import Base
from app.models import Occasion, Reminder, ReminderStatus, User
from app.models.celebrant_friend import CelebrantFriend
from app.models.celebrant_occasion import CelebrantOccasion


@pytest.fixture
async def celebrant_db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Occasion.__table__,
                Reminder.__table__,
                CelebrantOccasion.__table__,
                CelebrantFriend.__table__,
            ],
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _pending_reminder_count(db: AsyncSession, occasion_id: str, phone: str) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(Reminder)
        .where(
            Reminder.occasion_id == occasion_id,
            Reminder.target_phone == phone,
            Reminder.status == ReminderStatus.PENDING,
        )
    )


@pytest.mark.asyncio
async def test_revived_occasion_reschedules_existing_friend(celebrant_db: AsyncSession) -> None:
    db = celebrant_db
    owner_phone = "919000000021"
    friend_clean = "919876543210"
    db.add(User(id="usr_rev", phone=owner_phone, name="Karan"))
    await db.flush()

    # Occasion 40 days out so reminders land in the future (not tight-window).
    future = datetime.now(timezone.utc) + timedelta(days=40)
    args = {
        "occasion_type": "BIRTHDAY",
        "date_day": future.day,
        "date_month": future.month,
        "preferences": ["books"],
        "friend_phones": [friend_clean],
    }

    created = await setup_celebrant_occasion(db, owner_phone, args)
    occ_id = created["occasion_id"]
    assert await _pending_reminder_count(db, occ_id, friend_clean) > 0

    # Soft-delete: purges all reminders, keeps the CelebrantFriend row.
    await delete_occasion(db, owner_phone, {"occasion_id": occ_id})
    assert await _pending_reminder_count(db, occ_id, friend_clean) == 0

    # Revive by setting the same occasion up again with the SAME friend — that
    # friend is "existing", not new, so the old bug scheduled nothing for them.
    revived = await setup_celebrant_occasion(db, owner_phone, args)
    assert revived["occasion_id"] == occ_id  # same row revived, not a new one
    assert await _pending_reminder_count(db, occ_id, friend_clean) > 0


@pytest.mark.asyncio
async def test_live_additive_merge_does_not_duplicate_existing_friend(
    celebrant_db: AsyncSession,
) -> None:
    """The revival fix must NOT double-schedule on a normal (non-deleted) merge:
    re-running setup on a LIVE occasion with the same friend adds no reminders."""
    db = celebrant_db
    owner_phone = "919000000022"
    friend_clean = "919876543211"
    db.add(User(id="usr_live", phone=owner_phone, name="Karan"))
    await db.flush()

    future = datetime.now(timezone.utc) + timedelta(days=40)
    args = {
        "occasion_type": "BIRTHDAY",
        "date_day": future.day,
        "date_month": future.month,
        "preferences": ["books"],
        "friend_phones": [friend_clean],
    }

    created = await setup_celebrant_occasion(db, owner_phone, args)
    occ_id = created["occasion_id"]
    first_count = await _pending_reminder_count(db, occ_id, friend_clean)
    assert first_count > 0

    # Re-run on the LIVE occasion (never deleted) with the same friend.
    await setup_celebrant_occasion(db, owner_phone, args)
    assert await _pending_reminder_count(db, occ_id, friend_clean) == first_count


@pytest.mark.asyncio
async def test_celebrant_can_save_occasion_before_adding_preferences_or_friends(
    celebrant_db: AsyncSession,
) -> None:
    db = celebrant_db
    owner_phone = "919000000023"
    db.add(User(id="usr_progressive", phone=owner_phone, name="Karan"))
    await db.flush()

    future = datetime.now(timezone.utc) + timedelta(days=60)
    created = await setup_celebrant_occasion(
        db,
        owner_phone,
        {
            "occasion_type": "BIRTHDAY",
            "date_day": future.day,
            "date_month": future.month,
        },
    )

    assert created["success"] is True
    assert created["preferences"] == []
    assert created["friends_count"] == 0
    assert created["recurring"] is True
    assert "add gift preferences" in created["message"]
