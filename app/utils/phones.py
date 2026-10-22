"""Canonical phone identities shared by checkout, mobile and WhatsApp."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


def normalize_phone(raw: str) -> str:
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits or raw


def phone_aliases(raw: str) -> tuple[str, ...]:
    canonical = normalize_phone(raw)
    return tuple(dict.fromkeys((canonical, "+" + canonical, raw.strip())))


async def user_for_phone(db: AsyncSession, phone: str) -> User | None:
    # Keep existing account IDs and sessions valid. Prefer a registered profile
    # when pre-existing +/- duplicates exist; never merge records implicitly.
    return await db.scalar(
        select(User)
        .where(User.phone.in_(phone_aliases(phone)))
        .order_by(func.coalesce(User.is_guest, False).asc(), User.created_at, User.id)
        .limit(1)
    )
