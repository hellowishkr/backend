"""Shared read-only DB validation helpers for tool-adjacent workflows.

These helpers centralize small ownership/access lookups used by adapter.py,
cron jobs, and webhook handlers. They are intentionally side-effect free:
no commits, no structured adapter errors, and no user-facing messages.

Missing or unauthorized rows return ``None``/``False``; callers decide how to
surface that outcome for their own context.

User lookups accept canonical numbers and legacy leading-plus aliases so
checkout identities remain accessible from WhatsApp.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, Occasion, User
from app.models.celebrant_friend import CelebrantFriend
from app.utils.phones import normalize_phone  # noqa: F401 - compatibility re-export
from app.utils.phones import phone_aliases, user_for_phone


from app.models.celebrant_occasion import CelebrantOccasion


async def current_user(db: AsyncSession, phone: str) -> User | None:
    """Return the user for ``phone``, or ``None`` when not registered."""
    return await user_for_phone(db, phone)


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
        .where(User.phone.in_(phone_aliases(phone)), Occasion.id == occasion_id, Occasion.deleted_at.is_(None))
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
        .where(User.phone.in_(phone_aliases(phone)), Contact.id == contact_id)
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
