"""Admin occasions — upcoming user occasions (birthdays, anniversaries, festivals).

Read-only listing over the existing `Occasion` model (already used by the
WhatsApp reminder job). Sorted by days until next occurrence, computed by
rolling each occasion's month/day into the current or next IST year.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.admin_catalog import require_role
from app.db.session import get_db
from app.models.contact import Contact
from app.models.occasion import Occasion, OccasionType, occasion_phrase
from app.models.user import User
from app.utils.dates import next_occurrence

router = APIRouter(prefix="/admin/occasions", tags=["admin-occasions"])

# Intentional India-operations calendar for this admin view. User-facing
# occasion and reminder calculations use each user's saved timezone.
_IST = ZoneInfo("Asia/Kolkata")


def _month_day_values(start: date, end: date) -> list[str]:
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime("%m-%d"))
        if cursor.month == 2 and cursor.day == 28:
            days.append("02-29")
        cursor += timedelta(days=1)
    return sorted(set(days))


def _month_day_expr(db: AsyncSession):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return func.strftime("%m-%d", Occasion.date)
    return func.to_char(Occasion.date, "MM-DD")


def _occasion_label(occasion: Occasion) -> str:
    if occasion.contact is None:
        return occasion_phrase(occasion.type, occasion.custom_label)

    if occasion.type == OccasionType.CUSTOM:
        base = (
            occasion.custom_label.strip()
            if occasion.custom_label and occasion.custom_label.strip()
            else "special day"
        )
    else:
        base = occasion_phrase(occasion.type, occasion.custom_label)
        if base.startswith("your "):
            base = base.removeprefix("your ")
        elif base.startswith("the "):
            base = base.removeprefix("the ")
    return f"{occasion.contact.name}'s {base}"


def serialize_occasion(occasion: Occasion, user: User, next_date: date, days_until: int) -> dict:
    return {
        "occasion_id": occasion.id,
        "user_id": user.id,
        "user_name": user.name,
        "user_phone": user.phone,
        "contact_id": occasion.contact_id,
        "contact_name": occasion.contact.name if occasion.contact else None,
        "type": occasion.type.value,
        "label": _occasion_label(occasion),
        "date": occasion.date.isoformat(),
        "recurring": occasion.recurring,
        "next_occurrence": next_date.isoformat(),
        "days_until": days_until,
    }


@router.get("", dependencies=[Depends(require_role("owner", "ops"))])
async def list_upcoming_occasions(
    db: AsyncSession = Depends(get_db),
    type: OccasionType | None = Query(default=None),
    within_days: int = Query(default=30, ge=1, le=365),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    now = datetime.now(_IST)
    today = now.date()
    window_end = today + timedelta(days=within_days)
    window_end_dt = datetime.combine(window_end + timedelta(days=1), datetime.min.time(), tzinfo=_IST)

    month_days = _month_day_values(today, window_end)
    recurring_window = and_(
        Occasion.recurring.is_(True),
        _month_day_expr(db).in_(month_days),
    )
    fixed_date_window = and_(
        Occasion.recurring.is_(False),
        Occasion.date >= now,
        Occasion.date < window_end_dt,
    )

    stmt = (
        select(Occasion)
        .options(selectinload(Occasion.user), selectinload(Occasion.contact))
        .where(
            Occasion.deleted_at.is_(None),
            or_(Occasion.snooze_until.is_(None), Occasion.snooze_until <= now),
            or_(Occasion.skipped_year.is_(None), Occasion.skipped_year != str(today.year)),
            or_(recurring_window, fixed_date_window),
        )
    )
    if type is not None:
        stmt = stmt.where(Occasion.type == type)
    if q:
        like = f"%{q}%"
        stmt = (
            stmt.join(User, Occasion.user_id == User.id)
            .outerjoin(Contact, Occasion.contact_id == Contact.id)
            .where(
                or_(
                    User.name.ilike(like),
                    User.phone.ilike(like),
                    Contact.name.ilike(like),
                    Contact.phone.ilike(like),
                )
            )
        )

    occasions = (await db.execute(stmt)).scalars().unique().all()

    upcoming = []
    for occasion in occasions:
        if occasion.user is None:
            continue
        next_dt = next_occurrence(occasion, now)
        next_date = next_dt.astimezone(_IST).date()
        days_until = (next_date - today).days
        if days_until < 0:
            continue
        if days_until > within_days:
            continue
        upcoming.append((days_until, occasion, next_date))

    upcoming.sort(key=lambda row: row[0])
    total = len(upcoming)
    start = (page - 1) * limit
    page_rows = upcoming[start : start + limit]

    return {
        "occasions": [
            serialize_occasion(occasion, occasion.user, next_date, days_until)
            for days_until, occasion, next_date in page_rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }
