"""Shared read-only DB validation helpers for tool-adjacent workflows.

These helpers centralize small ownership/access lookups used by adapter.py,
cron jobs, and webhook handlers. They are intentionally side-effect free:
no commits, no structured adapter errors, and no user-facing messages.

Missing or unauthorized rows return ``None``/``False``; callers decide how to
surface that outcome for their own context.

Phone format assumption: all phone strings reaching this layer are already
normalized to 12-digit numeric strings (e.g. ``919876543210``). Normalization
happens at the WhatsApp webhook boundary and in setup_celebrant_occasion before
friend phones are persisted. Raw string comparison is therefore correct.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, Occasion, User
from app.models.celebrant_friend import CelebrantFriend


def normalize_phone(raw: str) -> str:
    """Normalize a phone string to a 12-digit Indian number (91XXXXXXXXXX).

    Handles the three formats the model commonly produces:
      - 10 digits (9359615424)      → 919359615424
      - 12 digits starting with 91  → unchanged
      - 11 digits starting with 0   → strip leading 0, prepend 91

    Any other format is returned as-is so callers can decide how to handle
    it rather than silently producing a wrong number.
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits or raw
from app.models.celebrant_occasion import CelebrantOccasion


async def current_user(db: AsyncSession, phone: str) -> User | None:
    """Return the user for ``phone``, or ``None`` when not registered."""
    result = await db.execute(select(User).where(User.phone == phone))
    return result.scalar_one_or_none()


async def owned_occasion(
    db: AsyncSession, phone: str, occasion_id: str
) -> Occasion | None:
    """Return an occasion only when it belongs to the user with ``phone``.

    Uses a single JOIN query rather than separate user + occasion lookups.
    """
    if not occasion_id:
        return None
    result = await db.execute(
        select(Occasion)
        .join(User, User.id == Occasion.user_id)
        .where(User.phone == phone, Occasion.id == occasion_id, Occasion.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def contact_owned(
    db: AsyncSession, phone: str, contact_id: str
) -> Contact | None:
    """Return a contact only when it belongs to the user with ``phone``.

    Uses a single JOIN query rather than separate user + contact lookups.
    """
    if not contact_id:
        return None
    result = await db.execute(
        select(Contact)
        .join(User, User.id == Contact.user_id)
        .where(User.phone == phone, Contact.id == contact_id)
    )
    return result.scalar_one_or_none()


async def celebrant_for_occasion(
    db: AsyncSession, occasion_id: str
) -> CelebrantOccasion | None:
    """Return the celebrant setup row for an occasion, if one exists.

    CelebrantOccasion.occasion_id carries a DB-level unique constraint, so
    scalar_one_or_none() will never raise on duplicates — they cannot exist.
    """
    result = await db.execute(
        select(CelebrantOccasion).where(CelebrantOccasion.occasion_id == occasion_id)
    )
    return result.scalar_one_or_none()


async def friend_linked(
    db: AsyncSession, cel_occ: CelebrantOccasion, target_phone: str
) -> bool:
    """Return whether ``target_phone`` is linked to the celebrant occasion."""
    result = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == target_phone,
        )
    )
    return result.scalar_one_or_none() is not None


async def invited_celebrant_friend(
    db: AsyncSession, phone: str, cel_occ_id: str
) -> tuple[CelebrantOccasion | None, bool]:
    """Return the celebrant occasion and whether ``phone`` is invited."""
    cel_occ = await db.get(CelebrantOccasion, cel_occ_id) if cel_occ_id else None
    if cel_occ is None:
        return None, False
    result = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == phone,
        )
    )
    return cel_occ, result.scalar_one_or_none() is not None


async def caller_can_access_occasion(
    db: AsyncSession, phone: str, occasion_id: str
) -> bool:
    """Return whether caller owns the occasion or is an invited friend."""
    if await owned_occasion(db, phone, occasion_id) is not None:
        return True
    cel_occ = await celebrant_for_occasion(db, occasion_id)
    return bool(cel_occ and await friend_linked(db, cel_occ, phone))
