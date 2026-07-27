"""Occasion-date helpers.

`next_occurrence` rolls recurring birthdays/anniversaries to the next future
instance so past-dated rows still show as upcoming.
"""

from datetime import date, datetime, timezone
from app.models.occasion import Occasion, OccasionType

_RECURRING_TYPES = {
    OccasionType.BIRTHDAY,
    OccasionType.ANNIVERSARY,
    OccasionType.DIWALI,
    OccasionType.RAKSHA_BANDHAN,
    OccasionType.VALENTINES,
    OccasionType.HOLI,
    OccasionType.EID,
    OccasionType.CHRISTMAS,
    OccasionType.NEW_YEAR,
    OccasionType.MOTHERS_DAY,
    OccasionType.FATHERS_DAY,
}


def next_occurrence(
    occasion: Occasion,
    now: datetime | None = None,
    *,
    reference_date: date | None = None,
) -> datetime:
    """Return the next calendar occurrence for this occasion.

    Non-recurring (or recurring=False): returns the stored date as-is.
    Recurring and past: rolls the year forward until its calendar date is at
    least ``reference_date`` (or the date portion of ``now``).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    base = occasion.date
    if base.tzinfo is None and now.tzinfo is not None:
        base = base.replace(tzinfo=now.tzinfo)
    elif base.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=base.tzinfo)
    today = reference_date or now.date()
    if base.date() >= today:
        return base

    if not occasion.recurring or occasion.type not in _RECURRING_TYPES:
        return base

    year = today.year
    while True:
        try:
            candidate = base.replace(year=year)
        except ValueError:
            # Feb 29 on non-leap year — fall back to Feb 28
            candidate = base.replace(year=year, day=28)
        if candidate.date() >= today:
            return candidate
        year += 1
