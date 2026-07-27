"""Reminder scheduling — create reminder records when an occasion is saved.

Policy (see documentation/issues/009-agent-reliability-overhaul.md §5b):

* Standard schedule is {-10, -5, -2, 0} days before the occasion. The -10
  window is the "gift planning" buffer that lets friends order from Amazon /
  Flipkart etc. with time for shipping.
* When an occasion is saved inside that planning window (``days_until <
  MIN_PLANNING_DAYS``), the earliest one or two reminders in the schedule
  would land in the past. The old code silently ``continue``-d past those
  and the user was never warned — so friends got no early heads-up and the
  user never knew.
* Past-scheduled reminder slots are skipped. Saving an occasion must never
  create an unsolicited immediate message. ``schedule_reminders`` returns a
  summary dict so callers (e.g. ``add_occasion`` and
  ``setup_celebrant_occasion``) can explain which default slots were missed
  and offer a user-selected schedule.
"""

from datetime import datetime, timezone, timedelta
from typing import Sequence, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Occasion, Reminder, ReminderStatus, User
from app.utils.id_gen import generate_id
from app.utils.timezones import (
    local_schedule_utc,
    occasion_calendar_date,
    today_for_user,
)


# Tunable in config if needed, but the default is load-bearing — changing it
# without also updating the user-facing copy will mislead customers.
REMINDER_SCHEDULE_DAYS = (10, 5, 2, 0)
MIN_PLANNING_DAYS = 10  # below this → tight-window warning to the user


_MESSAGE_TYPE_BY_DAYS = {
    10: "early_reminder",
    5: "gift_card_reminder",
    2: "last_minute_reminder",
    1: "one_day_before_reminder",
    0: "day_of_reminder",
}
# Greeting sent to the celebrant themselves on the day of their occasion
# (e.g. "Happy birthday!"). Distinct from ``day_of_reminder`` which goes
# to a user about a *contact's* occasion.
CELEBRANT_GREETING_TYPE = "celebrant_greeting"

class ReminderScheduleSummary(TypedDict):
    reminders_created: int
    tight_window: bool
    days_until: int
    skipped_past: int


async def schedule_celebrant_greeting(db: AsyncSession, occasion: Occasion) -> bool:
    """Schedule a day-of greeting to the celebrant themselves.

    Idempotent: if a row already exists for this occasion with
    ``message_type=CELEBRANT_GREETING_TYPE`` and ``target_phone IS NULL``,
    no new row is added. Safe to call repeatedly from
    ``setup_celebrant_occasion`` even when the merge path runs.

    Returns True if a row was inserted, False if already scheduled.
    """
    from sqlalchemy import select

    occasion_date = occasion.date
    now = datetime.now(timezone.utc)
    user = await db.get(User, occasion.user_id)

    # Day-of at 09:00 in the occasion owner's saved timezone.
    scheduled = local_schedule_utc(occasion_date, user)
    if scheduled < now:
        scheduled = now

    existing_q = (
        select(Reminder.id)
        .where(
            Reminder.occasion_id == occasion.id,
            Reminder.message_type == CELEBRANT_GREETING_TYPE,
            Reminder.target_phone.is_(None),
        )
        .limit(1)
    )
    if (await db.execute(existing_q)).scalar_one_or_none():
        return False

    db.add(Reminder(
        id=generate_id("rem"),
        occasion_id=occasion.id,
        scheduled_at=scheduled,
        status=ReminderStatus.PENDING,
        message_type=CELEBRANT_GREETING_TYPE,
        target_phone=None,
    ))
    await db.flush()
    return True


async def schedule_reminders(
    db: AsyncSession,
    occasion: Occasion,
    target_phones: Sequence[str | None] | None = None,
) -> ReminderScheduleSummary:
    """Create reminder rows for an occasion.

    ``target_phones``: if provided, creates a parallel reminder set per phone
    (celebrant flow — friends get reminded). If ``None``, one set is created
    with ``target_phone=None`` (existing flow — the user themselves get
    reminded).

    Returns a dict for callers to surface to the agent:

    ```
    {
        "reminders_created": int,     # total Reminder rows inserted
        "tight_window":      bool,    # occasion is inside MIN_PLANNING_DAYS
        "days_until":        int,     # days between now and the occasion
        "skipped_past":      int,     # how many standard reminders were
                                      # already past and therefore skipped
    }
    ```

    The dict is advisory — callers may ignore it. But for user-facing
    occasions (``add_occasion`` / ``setup_celebrant_occasion``), surfacing
    the ``tight_window`` flag to the agent lets the bot warn the user
    that shipping windows are tight.
    """
    occasion_date = occasion.date
    now = datetime.now(timezone.utc)
    user = await db.get(User, occasion.user_id)
    days_until = (
        occasion_calendar_date(occasion_date)
        - today_for_user(user, now)
    ).days

    phones = list(target_phones) if target_phones is not None else [None]

    total_created = 0
    total_skipped_past = 0

    for phone in phones:
        for days_before in REMINDER_SCHEDULE_DAYS:
            message_type = _MESSAGE_TYPE_BY_DAYS[days_before]
            scheduled = local_schedule_utc(
                occasion_date - timedelta(days=days_before),
                user,
            )

            if scheduled <= now:
                # Never turn a missed slot into an unsolicited "send now".
                # The user can explicitly request an immediate reminder or
                # replace the schedule with offsets that are still useful.
                total_skipped_past += 1
                continue

            db.add(Reminder(
                id=generate_id("rem"),
                occasion_id=occasion.id,
                scheduled_at=scheduled,
                status=ReminderStatus.PENDING,
                message_type=message_type,
                target_phone=phone,
            ))
            total_created += 1

    await db.flush()

    return {
        "reminders_created": total_created,
        "tight_window": days_until < MIN_PLANNING_DAYS,
        "days_until": days_until,
        "skipped_past": total_skipped_past,
    }
