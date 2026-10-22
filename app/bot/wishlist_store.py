"""DB-backed wishlist store.

Public API:
- add_wishlist_product_url(db, phone, raw_url) -> "new" | "duplicate" | "limit_reached"
- list_wishlist_items(db, phone) -> list[WishlistItemView]

Dedup strategy (mirrored by DB unique indexes):
1. (user_id, product_id) when product_id is non-null.
2. (user_id, normalized_url).

The application-level dedup catches the common case in one query. The DB
indexes are the safety net for races (two webhook deliveries on the same
phone within ms — Postgres rejects the second insert, we surface "duplicate").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, User, WishlistItem
from app.utils.id_gen import generate_id
from app.utils.phones import normalize_phone, user_for_phone
from app.utils.link_utils import extract_product_id, guess_product_name, normalize_url

logger = logging.getLogger("wishkr.wishlist")

Outcome = Literal["new", "duplicate", "limit_reached"]

# Product cap per user. Enforced in Python; no DB constraint (a hard count
# trigger would make legit edits painful — Python check is sufficient).
MAX_ITEMS_PER_USER = 50

# Sentinel so list_wishlist_items can distinguish "caller did not pass
# own_occasion_id" (skip the filter) from "caller passed None" (filter
# to items with no own_occasion_id).
_UNSET = object()


@dataclass(frozen=True)
class WishlistItemView:
    """Lightweight read model for the router / formatter."""

    title: str
    url: str
    product_id: str | None


async def _get_or_create_user(db: AsyncSession, phone: str) -> User:
    """Look up by phone; lazily create a guest row if missing.

    Wishlist saves can happen before registration (user pastes a link before
    onboarding). Mirrors the guest-user pattern used in the friend-purchase
    flow (see `app/bot/tools/functions.py`).
    """
    phone = normalize_phone(phone)
    user = await user_for_phone(db, phone)
    if user is not None:
        return user

    user = User(id=generate_id("usr"), phone=phone, is_guest=True)
    db.add(user)
    await db.flush()
    logger.info("[%s] wishlist: created guest user %s", phone, user.id)
    return user


async def _find_duplicate(
    db: AsyncSession,
    user_id: str,
    product_id: str | None,
    normalized_url: str,
    purpose: str,
    recipient_occasion_id: str | None,
) -> WishlistItem | None:
    """Return an existing row that should block this insert, or None.

    Dedup is scoped per (purpose, recipient_occasion_id) so the same URL
    can exist as a self-save and as a gift idea for a friend without
    collision — matches the unique index on the table.
    """
    conditions = [WishlistItem.url == normalized_url]
    if product_id:
        conditions.append(WishlistItem.product_id == product_id)
    recipient_match = (
        WishlistItem.recipient_occasion_id == recipient_occasion_id
        if recipient_occasion_id is not None
        else WishlistItem.recipient_occasion_id.is_(None)
    )
    result = await db.execute(
        select(WishlistItem)
        .where(
            WishlistItem.user_id == user_id,
            WishlistItem.purpose == purpose,
            recipient_match,
            or_(*conditions),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _count_items(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(
        select(func.count(WishlistItem.id)).where(WishlistItem.user_id == user_id)
    )
    return int(result.scalar_one() or 0)


async def add_wishlist_product_url(
    db: AsyncSession,
    phone: str,
    raw_url: str,
    *,
    purpose: str = "self",
    recipient_occasion_id: str | None = None,
    own_occasion_id: str | None = None,
    visibility: str = "private",
    remind_on: "date | None" = None,
) -> Outcome:
    """Persist one product URL; report whether it was new, dup, or rejected.

    `purpose` is 'self' (user wants it for themselves) or 'gift' (saving
    as a gift idea for someone else's occasion). When 'gift',
    `recipient_occasion_id` should be set to the `occ_*`/`cel_*` id.

    `own_occasion_id` links a self-wishlist item to the user's own upcoming
    occasion (birthday, wedding, etc.) for occasion-scoped personal wishlists.
    Only used when purpose='self'.

    `visibility` controls sharing: 'private' (default), 'friends', or 'public'.
    Only meaningful for purpose='self' items.
    """
    if purpose not in ("self", "gift"):
        purpose = "self"
    if purpose == "self":
        # Self-saves never carry a recipient. Drop any value passed in.
        recipient_occasion_id = None
    else:
        # Gift-saves never carry own_occasion_id or visibility.
        own_occasion_id = None
        visibility = "private"
    if visibility not in ("private", "friends", "public"):
        visibility = "private"

    normalized = normalize_url(raw_url)
    if not normalized:
        logger.warning(
            "[%s] wishlist add skipped — empty normalized url from %r",
            phone, raw_url[:80],
        )
        return "duplicate"

    product_id = extract_product_id(raw_url)
    title = (guess_product_name(raw_url) or "")[:160] or None
    if product_id and product_id.startswith("wishkr:"):
        product = await db.get(Product, product_id.removeprefix("wishkr:"))
        if product and product.name:
            title = product.name[:160]

    user = await _get_or_create_user(db, phone)

    existing = await _find_duplicate(
        db, user.id, product_id, normalized, purpose, recipient_occasion_id,
    )
    if existing is not None:
        logger.info(
            "[%s] wishlist duplicate user=%s pid=%s url=%s purpose=%s rcpt=%s",
            phone, user.id, product_id, normalized[:80], purpose, recipient_occasion_id,
        )
        return "duplicate"

    if await _count_items(db, user.id) >= MAX_ITEMS_PER_USER:
        logger.info("[%s] wishlist limit reached user=%s", phone, user.id)
        return "limit_reached"

    item = WishlistItem(
        id=generate_id("wsh"),
        user_id=user.id,
        product_id=product_id,
        url=normalized,
        title=title,
        purpose=purpose,
        recipient_occasion_id=recipient_occasion_id,
        own_occasion_id=own_occasion_id,
        visibility=visibility,
        remind_on=remind_on,
    )
    db.add(item)
    try:
        await db.flush()
    except IntegrityError:
        # Race — another concurrent insert won. Treat as a normal duplicate.
        await db.rollback()
        logger.info(
            "[%s] wishlist insert race resolved as duplicate user=%s pid=%s",
            phone, user.id, product_id,
        )
        return "duplicate"

    logger.info(
        "[%s] wishlist added user=%s id=%s pid=%s url=%s purpose=%s rcpt=%s occ=%s vis=%s",
        phone, user.id, item.id, product_id, normalized[:80],
        purpose, recipient_occasion_id, own_occasion_id, visibility,
    )
    return "new"


async def mark_gifted(
    db: AsyncSession,
    wishlist_item_id: str,
    *,
    order_id: str | None = None,
) -> bool:
    """Stamp gifted_at on the item so suggest_gifts stops recommending it.

    Idempotent — already-gifted items are a no-op. Returns True if a
    row was actually updated this call.
    """
    if not wishlist_item_id:
        return False
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(WishlistItem)
        .where(
            WishlistItem.id == wishlist_item_id,
            WishlistItem.gifted_at.is_(None),
        )
        .values(gifted_at=now, gifted_order_id=order_id)
    )
    updated = result.rowcount or 0
    if updated:
        logger.info(
            "wishlist marked gifted item=%s order=%s",
            wishlist_item_id, order_id,
        )
    return updated > 0


async def find_item_by_url(
    db: AsyncSession,
    user_id: str,
    raw_url: str,
) -> WishlistItem | None:
    """Look up a wishlist item by user + URL (any purpose). Used by order
    tools that accept a free-form URL hint and need to bind it to a row."""
    normalized = normalize_url(raw_url)
    if not normalized:
        return None
    result = await db.execute(
        select(WishlistItem)
        .where(
            WishlistItem.user_id == user_id,
            WishlistItem.url == normalized,
        )
        .order_by(WishlistItem.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_wishlist_items(
    db: AsyncSession,
    phone: str,
    *,
    include_gifted: bool = False,
    include_unavailable: bool = False,
    purpose: str | None = "self",
    own_occasion_id: object = _UNSET,
) -> list[WishlistItemView]:
    """Return wishlist items for a phone, oldest first. Empty if no user.

    `purpose` scopes the query:
    - 'self' (default): only the user's own personal wishlist items.
    - 'gift': only gift-plan items saved for someone else's occasion.
    - None: all purposes (use sparingly — audit/history views only).

    `own_occasion_id` optionally filters to a specific self-occasion:
    - _UNSET (default): no filter — all items regardless of occasion.
    - None: only items not linked to any occasion.
    - 'occ_xyz': only items linked to that specific occasion.

    By default, items already gifted (`gifted_at` set) and items the
    validation worker marked dead (`unavailable_at` set) are filtered
    out — callers showing a "what to buy" list never see stale or
    duplicate suggestions. Set the include_* flags to surface them.
    """
    result = await db.execute(select(User.id).where(User.phone == phone))
    user_id = result.scalar_one_or_none()
    if user_id is None:
        return []

    stmt = select(WishlistItem).where(WishlistItem.user_id == user_id)
    if purpose is not None:
        stmt = stmt.where(WishlistItem.purpose == purpose)
    if own_occasion_id is not _UNSET:
        if own_occasion_id is None:
            stmt = stmt.where(WishlistItem.own_occasion_id.is_(None))
        else:
            stmt = stmt.where(WishlistItem.own_occasion_id == own_occasion_id)
    if not include_gifted:
        stmt = stmt.where(WishlistItem.gifted_at.is_(None))
    if not include_unavailable:
        stmt = stmt.where(WishlistItem.unavailable_at.is_(None))
    rows = await db.execute(stmt.order_by(WishlistItem.created_at.asc()))
    items = rows.scalars().all()
    return [
        WishlistItemView(title=(it.title or ""), url=it.url, product_id=it.product_id)
        for it in items
    ]
