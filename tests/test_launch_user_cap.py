"""Limited-launch user cap on register_user.

New users beyond settings.launch_user_cap get the waitlist message instead of
a `users` row. Existing users and the cap-disabled case are unaffected.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.bot.tools.tool_impl.users import register_user
from app.core.config import settings
from app.db.base import Base
from app.models import User
from app.utils.id_gen import generate_id

T = TypeVar("T")


def _run_db(fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: Base.metadata.create_all(sc, tables=[User.__table__])
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


async def _seed_users(db: AsyncSession, n: int) -> None:
    for i in range(n):
        db.add(User(id=generate_id("usr"), phone=f"9100000{i:04d}", name=f"U{i}", is_guest=False))
    await db.flush()


def test_new_user_blocked_at_cap(monkeypatch):
    """A brand-new phone is waitlisted once the cap is reached; no row created."""
    monkeypatch.setattr(settings, "launch_user_cap", 3, raising=False)
    monkeypatch.setattr(settings, "waitlist_message", "WAITLIST_TEXT", raising=False)

    async def run(db: AsyncSession):
        await _seed_users(db, 3)  # at cap
        result = await register_user(db, phone="919999999999", args={"name": "Newbie", "city": ""})

        assert result["success"] is False
        assert result["waitlisted"] is True
        assert result["message"] == "WAITLIST_TEXT"
        # No new row was created.
        count = await db.scalar(select(func.count()).select_from(User))
        assert count == 3

    _run_db(run)


def test_new_user_allowed_below_cap(monkeypatch):
    """Under the cap, a new user registers normally."""
    monkeypatch.setattr(settings, "launch_user_cap", 3, raising=False)

    async def run(db: AsyncSession):
        await _seed_users(db, 2)  # one slot left
        result = await register_user(db, phone="919999999999", args={"name": "Newbie", "city": ""})

        assert result["success"] is True
        count = await db.scalar(select(func.count()).select_from(User))
        assert count == 3

    _run_db(run)


def test_existing_user_not_blocked_at_cap(monkeypatch):
    """An existing (e.g. guest) row completing registration is never waitlisted,
    even when the user count is at the cap."""
    monkeypatch.setattr(settings, "launch_user_cap", 3, raising=False)

    async def run(db: AsyncSession):
        await _seed_users(db, 2)
        # Pre-existing guest row (no name) — counts toward the 3 total.
        db.add(User(id=generate_id("usr"), phone="918888888888", name=None, is_guest=True))
        await db.flush()
        assert (await db.scalar(select(func.count()).select_from(User))) == 3  # at cap

        result = await register_user(db, phone="918888888888", args={"name": "Guest Now", "city": ""})
        assert result["success"] is True
        # Still 3 rows — we updated the existing one, didn't add.
        assert (await db.scalar(select(func.count()).select_from(User))) == 3

    _run_db(run)


def test_cap_disabled_with_zero(monkeypatch):
    """launch_user_cap=0 disables the cap entirely."""
    monkeypatch.setattr(settings, "launch_user_cap", 0, raising=False)

    async def run(db: AsyncSession):
        await _seed_users(db, 50)
        result = await register_user(db, phone="919999999999", args={"name": "Newbie", "city": ""})
        assert result["success"] is True
        assert (await db.scalar(select(func.count()).select_from(User))) == 51

    _run_db(run)
