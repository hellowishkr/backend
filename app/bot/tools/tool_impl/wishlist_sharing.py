"""Shared wishlist viewing, reservation, and purchase-confirmation tools.

These tools implement the friend-facing shared wishlist flow:
  get_friend_wishlist    — view a celebrant's shared wishlist items
  reserve_wishlist_item  — atomically claim an item before purchase
  confirm_wishlist_purchase — mark a reserved item as purchased

Privacy rules enforced here:
  - Only purpose='self' items with visibility='friends' are visible.
  - purpose='gift' items (the viewer's own gift-plan notes) are NEVER shown.
  - Purchaser identity is never revealed (surprise mode always on).
  - Share status is normalized before returning to the AI:
      'available'  → item is free to take
      'reserved'   → someone already claimed it; suggest another
      'purchased'  → already bought; do not suggest
      'expired'    → reservation lapsed; treat as available
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, WishlistItem, CelebrantOccasion, CelebrantFriend, Occasion
from app.utils.id_gen import generate_id
from app.bot.tools.tool_impl.helpers import _HINT_OCCASION_UNKNOWN, _HINT_CELEBRANT_UNKNOWN, _err

logger = logging.getLogger("wishkr.tools.wishlist_sharing")

# How long a reservation holds before it auto-expires and becomes available again.
RESERVATION_TTL_HOURS = 48


def _effective_status(item: WishlistItem, now: datetime) -> str:
    """Return the current effective share_status, expiring lapsed reservations."""
    status = item.share_status or "available"
    if status == "reserved" and item.reservation_expires_at:
        if now >= item.reservation_expires_at:
            return "expired"
    return status


async def _assert_friend_authorized(
    db: AsyncSession, phone: str, cel_occ_id: str
) -> CelebrantOccasion | None:
    """Return the CelebrantOccasion if phone is an invited friend, else None."""
    cel_occ = await db.scalar(
        select(CelebrantOccasion).where(CelebrantOccasion.id == cel_occ_id)
    )
    if not cel_occ:
        return None
    friend_row = await db.scalar(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ_id,
            CelebrantFriend.phone == phone,
        )
    )
    if friend_row is None:
        return None
    return cel_occ


async def get_friend_wishlist(db: AsyncSession, phone: str, args: dict) -> dict:
    """Return the celebrant's shared native Wishkr wishlist items.

    Only purpose='self' items with visibility='friends' are returned.
    Items with share_status='purchased' are shown as unavailable so the
    friend knows not to buy them, but purchaser identity is withheld.
    Expired reservations are treated as available.
    """
    cel_occ_id = (args.get("celebrant_occasion_id") or "").strip()
    if not cel_occ_id:
        return {"error": "celebrant_occasion_id_required"}

    cel_occ = await _assert_friend_authorized(db, phone, cel_occ_id)
    if cel_occ is None:
        return _err("not_invited", _HINT_CELEBRANT_UNKNOWN)

    occ = await db.get(Occasion, cel_occ.occasion_id)
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    celebrant_user = await db.get(User, occ.user_id)

    # Friends cannot view their own purpose='gift' items through this path.
    # The WHERE clause is precise: only the celebrant's OWN self-wishlist with
    # friends visibility. We never touch the calling friend's rows here.
    now = datetime.now(timezone.utc)
    rows = await db.execute(
        select(WishlistItem).where(
            WishlistItem.user_id == occ.user_id,
            WishlistItem.purpose == "self",
            WishlistItem.visibility == "friends",
            WishlistItem.unavailable_at.is_(None),
        ).order_by(WishlistItem.created_at.asc())
    )
    items = list(rows.scalars().all())

    if not items:
        return {
            "count": 0,
            "items": [],
            "celebrant_name": celebrant_user.name if celebrant_user else "your friend",
            "message": "No shared wishlist items found. They may have only shared external wishlist links.",
        }

    result_items = []
    for item in items:
        effective = _effective_status(item, now)
        # Map 'expired' → 'available' for external consumers (auto-heals reservations).
        display_status = "available" if effective == "expired" else effective
        result_items.append({
            "wishlist_item_id": item.id,
            "title": item.title or item.url,
            "url": item.url,
            "share_status": display_status,
            "available": display_status == "available",
            # Never reveal WHO reserved/purchased the item.
        })

    label = occ.custom_label or occ.type.value.replace("_", " ").title()
    return {
        "count": len(result_items),
        "items": result_items,
        "celebrant_name": celebrant_user.name if celebrant_user else "your friend",
        "occasion_label": label,
        "available_count": sum(1 for i in result_items if i["available"]),
        "note": (
            "Items with share_status='reserved' or 'purchased' are taken — "
            "suggest an available item instead. Never reveal who took an item."
        ),
    }


async def reserve_wishlist_item(db: AsyncSession, phone: str, args: dict) -> dict:
    """Atomically reserve a wishlist item for the calling friend.

    Uses SELECT FOR UPDATE (Postgres) / optimistic check (SQLite) to prevent
    two friends from reserving the same item simultaneously.
    """
    wishlist_item_id = (args.get("wishlist_item_id") or "").strip()
    cel_occ_id = (args.get("celebrant_occasion_id") or "").strip()
    if not wishlist_item_id:
        return {"error": "wishlist_item_id_required"}
    if not cel_occ_id:
        return {"error": "celebrant_occasion_id_required"}

    cel_occ = await _assert_friend_authorized(db, phone, cel_occ_id)
    if cel_occ is None:
        return _err("not_invited", _HINT_CELEBRANT_UNKNOWN)

    occ = await db.get(Occasion, cel_occ.occasion_id)
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    # Look up caller's user row for reservation identity.
    caller = await db.scalar(select(User).where(User.phone == phone))
    if not caller:
        return {"error": "caller_not_found"}

    # Fetch the item with a row-level lock to prevent concurrent reservations.
    try:
        item_q = (
            select(WishlistItem)
            .where(
                WishlistItem.id == wishlist_item_id,
                WishlistItem.user_id == occ.user_id,
                WishlistItem.purpose == "self",
                WishlistItem.visibility == "friends",
            )
            .with_for_update()
        )
        item = await db.scalar(item_q)
    except Exception:
        # SQLite doesn't support FOR UPDATE — fall back to optimistic check.
        item = await db.scalar(
            select(WishlistItem).where(
                WishlistItem.id == wishlist_item_id,
                WishlistItem.user_id == occ.user_id,
                WishlistItem.purpose == "self",
            )
        )

    if not item:
        return {
            "error": "item_not_found",
            "hint": "The wishlist item was not found or is not accessible. Call get_friend_wishlist to see current items.",
        }

    now = datetime.now(timezone.utc)
    effective = _effective_status(item, now)

    if effective == "purchased":
        return {
            "reserved": False,
            "conflict": True,
            "share_status": "purchased",
            "message": "This item has already been purchased. Please choose a different item.",
        }
    if effective == "reserved":
        return {
            "reserved": False,
            "conflict": True,
            "share_status": "reserved",
            "message": "This item is currently reserved by someone else. Please choose a different item.",
        }

    # Item is available or expired — claim it.
    expires = now + timedelta(hours=RESERVATION_TTL_HOURS)
    item.reserved_by_user_id = caller.id
    item.reserved_at = now
    item.reservation_expires_at = expires
    item.share_status = "reserved"
    db.add(item)
    await db.flush()

    logger.info(
        "wishlist item reserved item=%s by user=%s expires=%s",
        item.id, caller.id, expires.isoformat(),
    )
    return {
        "reserved": True,
        "wishlist_item_id": item.id,
        "share_status": "reserved",
        "reservation_expires_at": expires.isoformat(),
        "message": (
            f"Reserved! You have until {expires.strftime('%d %B %Y %H:%M UTC')} "
            "to complete the purchase. After that, the item becomes available again."
        ),
    }


async def confirm_wishlist_purchase(db: AsyncSession, phone: str, args: dict) -> dict:
    """Mark a reserved wishlist item as purchased by the calling friend.

    Only the friend who reserved the item may mark it as purchased.
    Permanently hides the item from the available pool.
    """
    wishlist_item_id = (args.get("wishlist_item_id") or "").strip()
    cel_occ_id = (args.get("celebrant_occasion_id") or "").strip()
    if not wishlist_item_id:
        return {"error": "wishlist_item_id_required"}
    if not cel_occ_id:
        return {"error": "celebrant_occasion_id_required"}

    cel_occ = await _assert_friend_authorized(db, phone, cel_occ_id)
    if cel_occ is None:
        return _err("not_invited", _HINT_CELEBRANT_UNKNOWN)

    occ = await db.get(Occasion, cel_occ.occasion_id)
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    caller = await db.scalar(select(User).where(User.phone == phone))
    if not caller:
        return {"error": "caller_not_found"}

    item = await db.scalar(
        select(WishlistItem).where(
            WishlistItem.id == wishlist_item_id,
            WishlistItem.user_id == occ.user_id,
        )
    )
    if not item:
        return {"error": "item_not_found"}

    now = datetime.now(timezone.utc)
    effective = _effective_status(item, now)

    if effective == "purchased":
        return {
            "purchased": False,
            "already_purchased": True,
            "message": "This item is already marked as purchased.",
        }

    if effective == "reserved" and item.reserved_by_user_id != caller.id:
        return {
            "purchased": False,
            "conflict": True,
            "message": "This item was reserved by a different friend. Only they can mark it as purchased.",
        }

    # Mark purchased — also stamp gifted_at for backward compatibility with
    # the existing suggest_gifts dedup logic that reads gifted_at.
    item.share_status = "purchased"
    item.purchased_by_user_id = caller.id
    item.purchased_at = now
    item.gifted_at = item.gifted_at or now
    db.add(item)
    await db.flush()

    logger.info(
        "wishlist item confirmed purchased item=%s by user=%s",
        item.id, caller.id,
    )
    return {
        "purchased": True,
        "wishlist_item_id": item.id,
        "share_status": "purchased",
        "message": "Marked as purchased! The celebrant will not know who bought it — your surprise is safe.",
    }
