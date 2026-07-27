"""Shared helpers used by 2+ domain modules.

No imports from other tool_impl modules — domain modules import this file,
not the other way around.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Contact, Occasion, User,
    CelebrantOccasion, CelebrantFriend,
)
from app.services.hubble import hubble_service
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.tools.functions")

def _err(code: str, hint: str, **extra) -> dict:
    """Structured tool error envelope.

    Gemini respects structured errors — the `hint` field typically tells the
    model which tool to call next to recover (e.g. 'call get_my_occasions
    first'), and the model follows that rather than confabulating a success.

    Keep `code` short & machine-friendly; `hint` is a one-line natural-language
    recovery instruction aimed at the model, not the user.
    """
    out = {"error": code, "hint": hint}
    out.update(extra)
    return out


# Canonical recovery hints keyed by error code. Centralising means a hint
# change (say, renaming a recovery tool) ripples through every error path.
_HINT_REGISTER = "Call register_user first with the user's name to onboard them before any other action."
_HINT_OCCASION_UNKNOWN = "The occasion_id is invalid or not owned by this user. Call get_my_occasions to list valid ids, then retry."
_HINT_BRAND_UNKNOWN = "This brand is not in our catalogue. Call search_brands with the user's query to get a valid brand_name, then retry."
_HINT_CELEBRANT_UNKNOWN = "The celebrant_occasion_id is invalid. Confirm the FriendContext id from turn context, or ask the user to re-open the reminder link."


async def _get_user(db: AsyncSession, phone: str) -> User | None:
    """Get user by phone number."""
    result = await db.execute(select(User).where(User.phone == phone))
    return result.scalar_one_or_none()


async def _find_or_create_contact(
    db: AsyncSession, user_id: str, name: str, phone: str | None, relationship: str | None,
) -> Contact:
    """Find existing contact or create a new one."""
    if phone:
        result = await db.execute(
            select(Contact).where(Contact.user_id == user_id, Contact.phone == phone)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

    contact = Contact(
        id=generate_id("con"), user_id=user_id, name=name,
        phone=phone, relationship_type=relationship,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _find_brand_id(brand_name: str, db: AsyncSession | None = None) -> str | None:
    """Find a Hubble brand ID by name (fuzzy match).

    Prefers ``gift_catalog`` (refreshed by ``sync_gift_catalog``) over the
    in-memory Hubble cache so the order path stays in sync with what the
    suggester showed the user. Falls back to live Hubble if the DB returned
    nothing or no session was provided (kept for legacy call sites).
    """
    brand_lower = brand_name.lower().strip()
    if not brand_lower:
        return None

    if db is not None:
        try:
            from app.models.gift_catalog import GiftCatalogItem
            result = await db.execute(
                select(GiftCatalogItem.external_id)
                .where(
                    GiftCatalogItem.partner == "hubble",
                    GiftCatalogItem.active.is_(True),
                    GiftCatalogItem.brand_name_lower.contains(brand_lower),
                )
                .limit(1)
            )
            row = result.first()
            if row:
                return row[0]
        except Exception:
            logger.exception("gift_catalog lookup failed for brand=%s, falling back to live", brand_name)

    try:
        all_brands = await hubble_service.get_brands()
    except Exception:
        return None

    for b in all_brands:
        if brand_lower in b.get("title", "").lower():
            return b["id"]
    return None


async def _check_brand_amount(brand_id: str, brand_name: str, amount: float) -> dict | None:
    """Return an error dict if amount is invalid for this brand, else None.

    Checks both FIXED denomination lists and FLEXIBLE min/max bounds so the
    bot can tell the user exactly what's available before any order row or
    payment link is created.
    """
    try:
        all_brands = await hubble_service.get_brands()
    except Exception:
        return None  # can't validate — let fulfillment catch it

    brand = next((b for b in all_brands if b.get("id") == brand_id), None)
    if not brand:
        return None

    restrictions = brand.get("amountRestrictions", {})
    denom_type = brand.get("denominationType", "FLEXIBLE")
    amount_int = int(amount)

    if denom_type == "FIXED":
        allowed = sorted(restrictions.get("denominations", []))
        if allowed and amount_int not in allowed:
            formatted = ", ".join(f"₹{d}" for d in allowed)
            return {
                "error": "invalid_denomination",
                "message": (
                    f"{brand_name} gift cards are only available in fixed amounts: {formatted}. "
                    f"Please choose one of these."
                ),
                "available_denominations": allowed,
                "denomination_type": "FIXED",
            }
    else:
        min_amt = restrictions.get("minAmount", 0)
        max_amt = restrictions.get("maxAmount", 0)
        if min_amt and amount_int < min_amt:
            return {
                "error": "amount_too_low",
                "message": (
                    f"The minimum amount for a {brand_name} gift card is ₹{min_amt}. "
                    f"Please choose ₹{min_amt} or more."
                ),
                "min_amount": min_amt,
                "max_amount": max_amt,
                "denomination_type": "FLEXIBLE",
            }
        if max_amt and amount_int > max_amt:
            return {
                "error": "amount_too_high",
                "message": (
                    f"The maximum amount for a {brand_name} gift card is ₹{max_amt}. "
                    f"Please choose ₹{max_amt} or less."
                ),
                "min_amount": min_amt,
                "max_amount": max_amt,
                "denomination_type": "FLEXIBLE",
            }

    return None


async def _user_owns_occasion(db: AsyncSession, occasion_id: str, user_id: str) -> Occasion | None:
    """Return the occasion iff this user owns it, else None.

    Ownership check prevents an authenticated user from reading or acting on
    another user's reminders via a guessed id.
    """
    occ = await db.get(Occasion, occasion_id)
    if occ is None or occ.user_id != user_id:
        return None
    return occ


async def _can_view_occasion_gifts(
    db: AsyncSession, occasion_id: str, phone: str
) -> Occasion | None:
    """Return the occasion iff the caller is the owner OR an invited friend.

    Used by tools that expose the gift-claim checklist for an occasion. The
    owner needs it for "what's been bought" UX; an invited friend needs it
    for the dedup-conflict UX. Anyone else gets ``None``, which the caller
    surfaces as ``occasion_not_found`` to avoid revealing existence.
    """
    occ = await db.get(Occasion, occasion_id)
    if occ is None:
        return None

    user = await _get_user(db, phone)
    if user is not None and occ.user_id == user.id:
        return occ

    # Friend membership: cel_occ for this occasion + caller phone in the
    # CelebrantFriend list. Inline so we don't re-fetch occ above.
    cel_occ_row = await db.execute(
        select(CelebrantOccasion).where(CelebrantOccasion.occasion_id == occ.id)
    )
    cel_occ = cel_occ_row.scalar_one_or_none()
    if cel_occ is None:
        return None
    friend_row = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == phone,
        )
    )
    if friend_row.scalar_one_or_none() is None:
        return None
    return occ


async def _duplicate_brand_warning(
    db: AsyncSession, occasion_id: str, brand_name: str
) -> dict | None:
    """Return a pause-and-confirm response when the exact brand is claimed.

    The bot is still allowed to proceed if the buyer explicitly says they want
    the duplicate anyway; pass ``duplicate_acknowledged=true`` on the next
    order call. This keeps duplicate prevention deterministic even when the
    model forgets to call ``check_occasion_gifts`` first.
    """
    from app.utils.dedup import get_full_gift_checklist
    brand_key = (brand_name or "").strip().lower()
    if not brand_key:
        return None

    checklist = await get_full_gift_checklist(db, occasion_id)
    taken = {str(b).strip().lower() for b in checklist.get("brands_taken", []) if b}
    if brand_key not in taken:
        return None

    return {
        "success": False,
        "duplicate_warning": True,
        "requires_duplicate_acknowledgement": True,
        "can_proceed": True,
        "brand": brand_name,
        "message": (
            f"{brand_name} already appears to be claimed for this occasion. "
            "Tell the buyer this may be a duplicate, suggest another option, "
            "and only call the order tool again with duplicate_acknowledged=true "
            "if they still want to proceed."
        ),
    }
