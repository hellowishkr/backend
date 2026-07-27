"""Soft-deleted occasions must vanish from owner-facing flows.

Regression for the leak where a deleted occasion still surfaced in the bot's
upcoming digest (and, by the same missing filter, the same-day dedup, monthly
summary, and action-router gift heuristic).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl import occasions
from app.models import Contact, Occasion, User
from app.models.occasion import OccasionType


async def _seed_friend_occasion(db: AsyncSession, phone: str) -> Occasion:
    user = User(id="usr_sd", phone=phone, name="Karan", city_raw="Pune")
    contact = Contact(id="con_sd", user_id=user.id, name="Asha", relationship_type="friend")
    # Two days out so it lands inside the default 7-day digest window.
    occ = Occasion(
        id="occ_sd",
        user_id=user.id,
        contact_id=contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc) + timedelta(days=2),
        recurring=True,
    )
    db.add_all([user, contact, occ])
    await db.flush()
    return occ


@pytest.mark.asyncio
async def test_upcoming_digest_hides_soft_deleted_occasion(db: AsyncSession) -> None:
    phone = "919000000009"
    occ = await _seed_friend_occasion(db, phone)

    # Visible before deletion.
    before = await occasions.get_upcoming_digest(db, phone, {"days": 7})
    assert any(item["occasion_id"] == occ.id for item in before["occasions"])

    # Soft-delete, then it must disappear from the digest.
    occ.deleted_at = datetime.now(timezone.utc)
    await db.flush()

    after = await occasions.get_upcoming_digest(db, phone, {"days": 7})
    assert all(item["occasion_id"] != occ.id for item in after["occasions"])
    assert after["total_upcoming"] == 0
