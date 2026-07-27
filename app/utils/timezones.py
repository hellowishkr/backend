"""User-timezone helpers for occasion calendar and reminder calculations."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE_NAME = "Asia/Kolkata"


def valid_timezone_name(value: str | None) -> str | None:
    """Return a normalized IANA timezone name, or None when invalid."""
    name = " ".join((value or "").strip().split())
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return name


def timezone_for_name(value: str | None) -> ZoneInfo:
    """Resolve an IANA name with the same safe fallback used for users."""
    return ZoneInfo(valid_timezone_name(value) or DEFAULT_TIMEZONE_NAME)


def timezone_for_user(user: Any) -> ZoneInfo:
    """Resolve a user's saved timezone with a safe India-default fallback."""
    return timezone_for_name(getattr(user, "timezone", None))


def today_for_user(user: Any, now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone_for_user(user)).date()


def occasion_calendar_date(value: datetime) -> date:
    """Occasion timestamps store calendar components; do not timezone-shift them."""
    return value.date()


def local_month_bounds_utc(
    user: Any,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the user's current local-month boundaries expressed in UTC."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(timezone_for_user(user))
    month_start_local = local_now.replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if month_start_local.month == 12:
        next_month_local = month_start_local.replace(
            year=month_start_local.year + 1,
            month=1,
        )
    else:
        next_month_local = month_start_local.replace(
            month=month_start_local.month + 1,
        )
    return (
        month_start_local.astimezone(timezone.utc),
        next_month_local.astimezone(timezone.utc),
    )


def local_schedule_utc(
    occasion_date: datetime,
    user: Any,
    *,
    hour: int = 9,
    minute: int = 0,
) -> datetime:
    """Render an occasion's calendar day at a user's local time in UTC."""
    local = datetime.combine(
        occasion_calendar_date(occasion_date),
        time(hour=hour, minute=minute),
        tzinfo=timezone_for_user(user),
    )
    return local.astimezone(timezone.utc)
