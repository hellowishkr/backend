from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl.celebrant import (
    setup_celebrant_occasion,
    update_celebrant_occasion,
)
from app.models import CelebrantFriend, Occasion, User


@pytest.mark.asyncio
async def test_progressive_setup_preserves_one_time_year_and_recurrence(
    db: AsyncSession,
) -> None:
    phone = "919000000801"
    db.add(User(id="usr_cel_progress", phone=phone, name="Swaraj"))
    await db.flush()

    created = await setup_celebrant_occasion(
        db,
        phone,
        {
            "occasion_type": "WEDDING",
            "date_day": 20,
            "date_month": 12,
            "date_year": 2027,
            "recurring": False,
        },
    )
    updated = await setup_celebrant_occasion(
        db,
        phone,
        {
            "occasion_type": "WEDDING",
            "date_day": 20,
            "date_month": 12,
            "preferences": ["Books"],
        },
    )

    occasion = await db.get(Occasion, created["occasion_id"])
    assert updated["merged"] is True
    assert occasion is not None
    assert occasion.date.date() == datetime(
        2027,
        12,
        20,
        tzinfo=timezone.utc,
    ).date()
    assert occasion.recurring is False


@pytest.mark.asyncio
async def test_update_celebrant_occasion_adds_and_removes_friends_without_date(
    db: AsyncSession,
) -> None:
    phone = "919000000811"
    old_friend = "919000000812"
    new_friend = "919000000813"
    db.add(User(id="usr_cel_friend_edit", phone=phone, name="Swaraj"))
    await db.flush()

    created = await setup_celebrant_occasion(
        db,
        phone,
        {
            "occasion_type": "WEDDING",
            "date_day": 20,
            "date_month": 12,
            "date_year": 2027,
            "recurring": False,
            "friend_phones": [old_friend],
        },
    )
    result = await update_celebrant_occasion(
        db,
        phone,
        {
            "occasion_id": created["occasion_id"],
            "preferences": ["Books", "Travel"],
            "add_friend_phones": [new_friend],
            "remove_friend_phones": [old_friend],
        },
    )

    friends = (
        await db.execute(
            select(CelebrantFriend).where(
                CelebrantFriend.celebrant_occasion_id
                == created["celebrant_occasion_id"]
            )
        )
    ).scalars().all()
    occasion = await db.get(Occasion, created["occasion_id"])

    assert result["friends_added"] == 1
    assert result["friends_removed"] == 1
    assert [friend.phone for friend in friends] == [new_friend]
    assert result["preferences"] == ["Books", "Travel"]
    assert occasion is not None
    assert occasion.date.year == 2027
    assert occasion.recurring is False
