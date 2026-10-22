from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import User, City
from app.utils.id_gen import generate_id
from app.utils.phones import normalize_phone, user_for_phone
from app.utils.cities import resolve_city_id
from app.utils.timezones import DEFAULT_TIMEZONE_NAME, valid_timezone_name


async def get_user_profile(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get the current user's profile."""
    user = await user_for_phone(db, phone)
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
    phone = normalize_phone(phone)
    existing = await user_for_phone(db, phone)
    timezone_input = (args.get("timezone") or "").strip()
    timezone_name = valid_timezone_name(timezone_input)
    if timezone_input and timezone_name is None:
        return {
            "success": False,
            "error": "Invalid timezone. Use an IANA name such as Asia/Kolkata.",
        }
    if existing and existing.name and not existing.is_guest:
        requested_name = (args.get("name") or "").strip()
        if requested_name and not 2 <= len(requested_name) <= 100:
            return {"success": False, "error": "Name must be between 2 and 100 characters"}
        if requested_name:
            existing.name = requested_name
            db.add(existing)
            await db.flush()
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

    if not 2 <= len(name) <= 100:
        return {"success": False, "error": "Name must be between 2 and 100 characters"}

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


async def set_chat_preferences(db: AsyncSession, phone: str, args: dict) -> dict:
    """Persist model-understood language preferences and clear abandoned flow state."""
    from app.bot.state_utils import clear_state, set_state

    result = {}
    language = args.get("language")
    if language is not None:
        if not isinstance(language, str) or not 2 <= len(language.strip()) <= 80 or any(ord(c) < 32 for c in language):
            return {"error": "invalid_language"}
        language = language.strip()
        await set_state(db, phone, "preferred_language", language)
        result["language"] = language
    if args.get("end_conversation") is True:
        for key in ("awaiting", "occasion_slots", "pending_occasion_confirmation", "active_gift_context", "budget"):
            await clear_state(db, phone, key)
        result["conversation_ended"] = True
    return {"success": True, **result}
