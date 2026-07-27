from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_occasions import list_upcoming_occasions
from app.db.base import Base
from app.models.contact import Contact
from app.models.occasion import Occasion, OccasionType
from app.models.user import User


@pytest.fixture
async def occasion_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, Contact.__table__, Occasion.__table__],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    db.add(User(id="usr_1", phone="9999999999", name="Asha Rao"))
    db.add(User(id="usr_2", phone="8888888888", name="Ravi Kumar"))
    # Recurring birthday 5 days from now (this year).
    soon = now + timedelta(days=5)
    db.add(
        Occasion(
            id="occ_1",
            user_id="usr_1",
            type=OccasionType.BIRTHDAY,
            date=soon,
            recurring=True,
        )
    )
    # Recurring anniversary that already passed this year -> should roll to next year (far out).
    passed = now - timedelta(days=10)
    db.add(
        Occasion(
            id="occ_2",
            user_id="usr_2",
            type=OccasionType.ANNIVERSARY,
            date=passed,
            recurring=True,
        )
    )
    # Non-recurring one-off event in the past -> excluded entirely.
    db.add(
        Occasion(
            id="occ_3",
            user_id="usr_2",
            type=OccasionType.WEDDING,
            date=passed,
            recurring=False,
        )
    )
    await db.commit()


async def test_list_upcoming_occasions_within_window(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        result = await list_upcoming_occasions(db=db, type=None, within_days=30, q=None, page=1, limit=100)

    assert result["total"] == 1
    occ = result["occasions"][0]
    assert occ["occasion_id"] == "occ_1"
    assert occ["user_id"] == "usr_1"
    assert 0 <= occ["days_until"] <= 6


async def test_list_upcoming_occasions_wide_window_excludes_nonrecurring_past(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        result = await list_upcoming_occasions(db=db, type=None, within_days=365, q=None, page=1, limit=100)

    ids = {occ["occasion_id"] for occ in result["occasions"]}
    assert ids == {"occ_1", "occ_2"}


async def test_list_upcoming_occasions_filters_by_type(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        result = await list_upcoming_occasions(
            db=db, type=OccasionType.BIRTHDAY, within_days=30, q=None, page=1, limit=100
        )

    assert len(result["occasions"]) == 1
    assert result["occasions"][0]["type"] == "BIRTHDAY"


async def test_list_upcoming_occasions_filters_by_query(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        result = await list_upcoming_occasions(db=db, type=None, within_days=30, q="asha", page=1, limit=100)
        assert len(result["occasions"]) == 1

        result = await list_upcoming_occasions(
            db=db, type=None, within_days=30, q="nonexistent", page=1, limit=100
        )
        assert len(result["occasions"]) == 0


async def test_list_upcoming_occasions_excludes_snoozed_and_skipped(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        now = datetime.now(timezone.utc)
        current_year = str(datetime.now(ZoneInfo("Asia/Kolkata")).year)
        db.add(
            Occasion(
                id="occ_snoozed",
                user_id="usr_1",
                type=OccasionType.BIRTHDAY,
                date=now + timedelta(days=3),
                recurring=True,
                snooze_until=now + timedelta(days=60),
            )
        )
        db.add(
            Occasion(
                id="occ_skipped",
                user_id="usr_1",
                type=OccasionType.BIRTHDAY,
                date=now + timedelta(days=4),
                recurring=True,
                skipped_year=current_year,
            )
        )
        await db.commit()

        result = await list_upcoming_occasions(db=db, type=None, within_days=30, q=None, page=1, limit=100)

    ids = {occ["occasion_id"] for occ in result["occasions"]}
    assert "occ_snoozed" not in ids
    assert "occ_skipped" not in ids


async def test_list_upcoming_occasions_uses_contact_identity_in_label(occasion_session_factory):
    async with occasion_session_factory() as db:
        await _seed(db)
        db.add(Contact(id="con_1", user_id="usr_1", name="Mom", phone="7777777777"))
        db.add(
            Occasion(
                id="occ_contact",
                user_id="usr_1",
                contact_id="con_1",
                type=OccasionType.BIRTHDAY,
                date=datetime.now(timezone.utc) + timedelta(days=2),
                recurring=True,
            )
        )
        await db.commit()

        result = await list_upcoming_occasions(db=db, type=None, within_days=30, q="mom", page=1, limit=100)

    assert result["total"] == 1
    occ = result["occasions"][0]
    assert occ["contact_id"] == "con_1"
    assert occ["contact_name"] == "Mom"
    assert occ["label"] == "Mom's birthday"
