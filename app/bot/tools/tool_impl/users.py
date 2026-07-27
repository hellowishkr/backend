from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import User, City
from app.utils.id_gen import generate_id
from app.utils.cities import resolve_city_id
from app.utils.timezones import DEFAULT_TIMEZONE_NAME, valid_timezone_name
from app.bot.tools.tool_impl.helpers import _get_user, _HINT_REGISTER, _err


async def get_user_profile(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get the current user's profile."""
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if not user:
        return {"registered": False, "message": "User not registered"}
    city_display = user.city_raw
    if not city_display and user.city_id:
        city = await db.get(City, user.city_id)
        city_display = city.name if city else None
    return {
        "registered": True,
        "name": user.name,
        "city": city_display,
        "timezone": user.timezone or DEFAULT_TIMEZONE_NAME,
        "is_guest": user.is_guest or False,
    }


async def register_user(db: AsyncSession, phone: str, args: dict) -> dict:
    """Register a new user. City is resolved via 3-tier match; never rejects non-empty input."""
    result = await db.execute(select(User).where(User.phone == phone))
    existing = result.scalar_one_or_none()
    timezone_input = (args.get("timezone") or "").strip()
    timezone_name = valid_timezone_name(timezone_input)
    if timezone_input and timezone_name is None:
        return {
            "success": False,
            "error": "Invalid timezone. Use an IANA name such as Asia/Kolkata.",
        }
    if existing and existing.name:
        if timezone_name:
            existing.timezone = timezone_name
            db.add(existing)
            await db.flush()
        return {
            "success": True,
            "message": "Already registered",
            "name": existing.name,
            "timezone": existing.timezone or DEFAULT_TIMEZONE_NAME,
        }

    name = args.get("name", "").strip()
    city_input = (args.get("city") or "").strip()

    if not name or len(name) < 2:
        return {"success": False, "error": "Name must be at least 2 characters"}

    resolution = await resolve_city_id(db, city_input)

    if existing:
        existing.name = name
        existing.city_id = resolution.city_id
        existing.city_raw = resolution.city_raw
        if timezone_name:
            existing.timezone = timezone_name
        existing.is_guest = False
        db.add(existing)
    else:
        # Limited-launch cap: only NEW users (no existing row) are gated.
        # Existing rows above complete normally. cap <= 0 disables the cap.
        if settings.launch_user_cap > 0:
            user_count = await db.scalar(select(func.count()).select_from(User))
            if (user_count or 0) >= settings.launch_user_cap:
                return {"success": False, "waitlisted": True, "message": settings.waitlist_message}
        user = User(
            id=generate_id("usr"), phone=phone, name=name,
            city_id=resolution.city_id, city_raw=resolution.city_raw,
            timezone=timezone_name or DEFAULT_TIMEZONE_NAME,
        )
        db.add(user)

    await db.flush()
    return {
        "success": True,
        "name": name,
        "city": resolution.city_raw,
        "timezone": (
            timezone_name
            or (existing.timezone if existing else DEFAULT_TIMEZONE_NAME)
        ),
    }
