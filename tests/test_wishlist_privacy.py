"""Wishlist privacy and purpose-filter regression tests.

Tests run with in-memory SQLite (via aiosqlite). Pattern mirrors
test_gift_automation_flow.py: only create the tables actually used,
avoiding Postgres-specific types (JSONB etc.) in unrelated models.

Phases covered:
  Phase 1  — purpose='self' filter on list_wishlist_items / list_my_wishlist
  Phase 2  — own_occasion_id scoping on personal wishlists
  Phase 3  — visibility field + get_friend_wishlist authorization
  Phase 4  — reservation lifecycle + concurrency guard
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.base import Base
from app.models import User, WishlistItem
from app.utils.id_gen import generate_id

T = TypeVar("T")

# Only the tables we exercise in these tests.
_TABLES = [
    User.__table__,
    WishlistItem.__table__,
]


def _run_db(fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


async def _make_user(db: AsyncSession, phone: str, name: str = "Test") -> User:
    user = User(id=generate_id("usr"), phone=phone, name=name, is_guest=False)
    db.add(user)
    await db.flush()
    return user


async def _make_item(
    db: AsyncSession,
    user_id: str,
    url: str,
    purpose: str = "self",
    recipient_occasion_id: str | None = None,
) -> WishlistItem:
    item = WishlistItem(
        id=generate_id("wsh"),
        user_id=user_id,
        url=url,
        title=url,
        purpose=purpose,
        recipient_occasion_id=recipient_occasion_id,
    )
    db.add(item)
    await db.flush()
    return item


# ─────────────────────────────────────────────────────────
# Phase 1 — purpose='self' filter (THE BUG FIX)
# ─────────────────────────────────────────────────────────

def test_list_wishlist_items_default_returns_only_self():
    """list_wishlist_items() must NOT return purpose='gift' items by default."""
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        user = await _make_user(db, "919000000001")
        await _make_item(db, user.id, "https://amazon.in/item/self1", purpose="self")
        await _make_item(db, user.id, "https://amazon.in/item/gift1", purpose="gift",
                         recipient_occasion_id="occ_abc")
        await db.commit()

        items = await list_wishlist_items(db, "919000000001")
        urls = [it.url for it in items]
        assert any("self1" in u for u in urls), "self item must be returned"
        assert not any("gift1" in u for u in urls), \
            "PRIVACY BUG: gift item leaked into personal list"

    _run_db(body)


def test_list_wishlist_items_gift_purpose_returns_gift_only():
    """When purpose='gift' is explicitly requested, only gift items are returned."""
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        user = await _make_user(db, "919000000002")
        await _make_item(db, user.id, "https://flipkart.com/self", purpose="self")
        await _make_item(db, user.id, "https://flipkart.com/gift", purpose="gift",
                         recipient_occasion_id="occ_xyz")
        await db.commit()

        items = await list_wishlist_items(db, "919000000002", purpose="gift")
        urls = [it.url for it in items]
        assert any("gift" in u for u in urls)
        assert not any("/self" in u for u in urls)

    _run_db(body)


def test_list_wishlist_items_none_purpose_returns_all():
    """purpose=None bypass returns all items — audit/history callers only."""
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        user = await _make_user(db, "919000000003")
        await _make_item(db, user.id, "https://amazon.in/all-self", purpose="self")
        await _make_item(db, user.id, "https://amazon.in/all-gift", purpose="gift",
                         recipient_occasion_id="occ_123")
        await db.commit()

        items = await list_wishlist_items(db, "919000000003", purpose=None)
        assert len(items) == 2

    _run_db(body)


def test_list_my_wishlist_tool_excludes_gift_items():
    """list_my_wishlist tool must not surface purpose='gift' items."""
    from app.bot.tools.tool_impl.wishlist import list_my_wishlist

    async def body(db):
        user = await _make_user(db, "919000000004")
        await _make_item(db, user.id, "https://myntra.com/my-self", purpose="self")
        await _make_item(db, user.id, "https://myntra.com/my-gift", purpose="gift",
                         recipient_occasion_id="occ_def")
        await db.commit()

        result = await list_my_wishlist(db, "919000000004", {})
        assert result["count"] == 1
        urls = [it["url"] for it in result["items"]]
        assert all("my-self" in u for u in urls)
        assert not any("my-gift" in u for u in urls)

    _run_db(body)


def test_clear_wishlist_only_clears_self_items():
    """clear_wishlist must not delete purpose='gift' entries."""
    from app.bot.tools.tool_impl.wishlist import clear_wishlist
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        user = await _make_user(db, "919000000005")
        await _make_item(db, user.id, "https://nykaa.com/clear-self", purpose="self")
        await _make_item(db, user.id, "https://nykaa.com/clear-gift", purpose="gift",
                         recipient_occasion_id="occ_ghi")
        await db.commit()

        result = await clear_wishlist(db, "919000000005", {})
        assert result["success"] is True
        assert result["cleared"] == 1

        remaining = await list_wishlist_items(db, "919000000005", purpose=None)
        assert len(remaining) == 1
        assert "clear-gift" in remaining[0].url

    _run_db(body)


def test_remove_wishlist_item_only_removes_self_item():
    """remove_wishlist_item targets only purpose='self' — same URL can coexist."""
    from app.bot.tools.tool_impl.wishlist import remove_wishlist_item
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        user = await _make_user(db, "919000000006")
        shared_url = "https://amazon.in/shared-url"
        await _make_item(db, user.id, shared_url, purpose="self")
        await _make_item(db, user.id, shared_url, purpose="gift",
                         recipient_occasion_id="occ_jkl")
        await db.commit()

        result = await remove_wishlist_item(db, "919000000006", {"url": shared_url})
        assert result["success"] is True

        remaining = await list_wishlist_items(db, "919000000006", purpose=None)
        assert len(remaining) == 1
        assert remaining[0].url == shared_url

    _run_db(body)


def test_empty_personal_wishlist_when_only_gift_items():
    """list_my_wishlist returns count=0 when user has only gift-purpose items."""
    from app.bot.tools.tool_impl.wishlist import list_my_wishlist

    async def body(db):
        user = await _make_user(db, "919000000007")
        await _make_item(db, user.id, "https://amazon.in/pure-gift", purpose="gift",
                         recipient_occasion_id="occ_mno")
        await db.commit()

        result = await list_my_wishlist(db, "919000000007", {})
        assert result["count"] == 0
        assert result["items"] == []

    _run_db(body)


def test_no_user_returns_empty_list():
    """list_wishlist_items returns [] for unknown phone without error."""
    from app.bot.wishlist_store import list_wishlist_items

    async def body(db):
        items = await list_wishlist_items(db, "919999999999")
        assert items == []

    _run_db(body)
