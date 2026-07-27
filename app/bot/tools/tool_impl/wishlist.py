from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tools.tool_impl.helpers import _get_user
from app.models import User, WishlistItem
from app.utils.timezones import today_for_user


async def add_to_wishlist(db: AsyncSession, phone: str, args: dict) -> dict:
    """LLM fallback for wishlist saves the regex preflight didn't catch.

    Accepts any URL the model passes. Lightly normalizes (adds https://),
    rejects obvious non-products via deny-list, then delegates to the
    same `add_wishlist_product_url` store the preflight uses — so dedupe,
    limits, and indexing behave identically across both paths.
    """
    from app.utils.link_utils import is_disallowed_link
    from app.bot.wishlist_store import add_wishlist_product_url

    raw_url = (args.get("url") or "").strip()
    if not raw_url:
        return {"error": "url_required", "hint": "Pass the product URL exactly as the user shared it."}

    purpose = (args.get("purpose") or "self").strip().lower()
    if purpose not in ("self", "gift"):
        return {
            "error": "invalid_purpose",
            "hint": "purpose must be 'self' (user wants this for themselves) or 'gift' (saving as a gift idea for someone else's occasion).",
        }

    recipient_occasion_id = (args.get("recipient_occasion_id") or "").strip() or None
    if purpose == "gift" and not recipient_occasion_id:
        return {
            "error": "recipient_required",
            "hint": "When purpose='gift', pass recipient_occasion_id — the occ_* or cel_* id of the celebration this gift is for.",
        }
    if purpose == "self":
        recipient_occasion_id = None

    own_occasion_id = (args.get("own_occasion_id") or "").strip() or None
    visibility = (args.get("visibility") or "private").strip().lower()
    if visibility not in ("private", "friends", "public"):
        visibility = "private"
    # purpose='gift' items have no personal occasion or visibility setting.
    if purpose == "gift":
        own_occasion_id = None
        visibility = "private"

    # Optional ad-hoc reminder date ("remind me on April 29 about this").
    # Worker scans rows where remind_on <= today AND reminded_at IS NULL.
    remind_on = None
    remind_on_raw = (args.get("remind_on") or "").strip()
    if remind_on_raw:
        from datetime import datetime as _dt

        try:
            remind_on = _dt.strptime(remind_on_raw, "%Y-%m-%d").date()
        except ValueError:
            return {
                "error": "invalid_remind_on",
                "hint": "remind_on must be ISO date YYYY-MM-DD (e.g. '2026-04-29'). Convert relative dates ('next Tuesday') yourself before calling.",
            }
        user = await _get_user(db, phone)
        if not user:
            return {"error": "user_not_found"}
        if remind_on < today_for_user(user):
            return {
                "error": "remind_on_in_past",
                "hint": "remind_on is in the past. Either drop it or pass a future date.",
            }

    # Permissive normalization — schemed URLs pass through; bare hosts
    # like `etsy.com/listing/123` get a scheme so urlparse works.
    if not raw_url.lower().startswith(("http://", "https://")):
        raw_url = f"https://{raw_url}"

    if is_disallowed_link(raw_url):
        return {
            "error": "not_a_product_link",
            "hint": "This looks like a social/video/news link, not a product page. Ask the user for the actual product URL.",
        }

    outcome = await add_wishlist_product_url(
        db, phone, raw_url,
        purpose=purpose,
        recipient_occasion_id=recipient_occasion_id,
        own_occasion_id=own_occasion_id,
        visibility=visibility,
        remind_on=remind_on,
    )
    if outcome == "duplicate":
        return {"success": False, "duplicate": True, "message": "Already in your wishlist."}
    if outcome == "limit_reached":
        return {"success": False, "limit_reached": True, "message": "Wishlist is full. Remove an item first."}

    return {"success": True, "message": "Saved to wishlist."}


async def remove_wishlist_item(db: AsyncSession, phone: str, args: dict) -> dict:
    """Remove a single item from the user's personal wishlist by URL."""
    from app.utils.link_utils import normalize_url
    user_result = await db.execute(select(User).where(User.phone == phone))
    user = user_result.scalar_one_or_none()
    if not user:
        return {"error": "user_not_found"}

    raw_url = (args.get("url") or "").strip()
    if not raw_url:
        return {"error": "url_required", "hint": "Pass the URL of the item to remove."}

    normalized = normalize_url(raw_url) or raw_url
    result = await db.execute(
        select(WishlistItem).where(
            WishlistItem.user_id == user.id,
            WishlistItem.purpose == "self",
            WishlistItem.url == normalized,
        ).limit(1)
    )
    item = result.scalar_one_or_none()
    if not item:
        return {"success": False, "message": "Item not found in your wishlist."}

    await db.delete(item)
    await db.flush()
    return {"success": True, "message": "Removed from wishlist."}


async def clear_wishlist(db: AsyncSession, phone: str, args: dict) -> dict:
    """Delete all personal wishlist items for the user."""
    user_result = await db.execute(select(User).where(User.phone == phone))
    user = user_result.scalar_one_or_none()
    if not user:
        return {"error": "user_not_found"}

    result = await db.execute(
        sa_delete(WishlistItem).where(
            WishlistItem.user_id == user.id,
            WishlistItem.purpose == "self",
        )
    )
    count = result.rowcount or 0
    await db.flush()
    if count == 0:
        return {"success": True, "cleared": 0, "message": "Wishlist was already empty."}
    return {"success": True, "cleared": count, "message": f"Cleared {count} item(s) from your wishlist."}


async def list_my_wishlist(db: AsyncSession, phone: str, args: dict) -> dict:
    """Return the user's personal wishlist items, optionally filtered by own occasion."""
    from app.bot.wishlist_store import list_wishlist_items, _UNSET
    # own_occasion_id filter: omit → all; pass '' or None → no occasion; pass id → that occasion.
    raw_occ = args.get("own_occasion_id")
    if raw_occ is None:
        own_occasion_id_filter = _UNSET
    else:
        own_occasion_id_filter = (raw_occ or "").strip() or None

    items = await list_wishlist_items(db, phone, own_occasion_id=own_occasion_id_filter)
    if not items:
        return {"count": 0, "items": [], "message": "Wishlist is empty."}
    return {
        "count": len(items),
        "items": [
            {"title": it.title or it.url, "url": it.url}
            for it in items
        ],
    }
