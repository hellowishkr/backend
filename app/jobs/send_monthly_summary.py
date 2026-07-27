"""Cron job: monthly occasion summary — local morning on the 1st.

Sends each user with at least one upcoming occasion this calendar month a
single summary via the approved ``monthly_occasion_summary`` utility
template (or richer free text when their CS window happens to be open):

    "Hi Karan, you have 3 occasions saved on Wishkr for June 2026.
     Next up: Aarav's Birthday on 12 Jun.
     Reply LIST to view all, or SKIP to pause reminders for this month."

LIST/SKIP replies are handled by the agent (``get_occasions`` /
``skip_occasion_this_year`` / ``snooze_occasion`` tools) — no keyword
router needed.

Users with zero occasions this month get nothing: sending the template
anyway would read as marketing and risks Meta reclassification (see
``monthly_summary_params`` docstring).

The scheduler invokes this job hourly. Each user is considered only during
09:00-09:59 on the first day of their saved timezone's month. Idempotence is
one send per (phone, local month), guarded by a Redis ``SET NX`` marker.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.bot.whatsapp_templates import (
    TEMPLATE_MONTHLY_OCCASION_SUMMARY,
    _first_name,
    monthly_summary_params,
)
from app.db.session import async_session
from app.models import Contact, Occasion, User
from app.services.whatsapp import send_text_or_template
from app.utils.dates import next_occurrence
from app.utils.observability import mask_phone
from app.utils.redis_client import get_redis
from app.utils.timezones import occasion_calendar_date, timezone_for_user

logger = logging.getLogger("wishkr.jobs.monthly_summary")


def _occasion_label(occ: Occasion) -> str:
    return occ.custom_label or occ.type.value.replace("_", " ").title()


def _local_month_context(user: User, now_utc: datetime) -> tuple[date, str, str] | None:
    local_now = now_utc.astimezone(timezone_for_user(user))
    if local_now.day != 1 or local_now.hour != 9:
        return None
    return (
        local_now.date(),
        local_now.strftime("%B %Y"),
        local_now.strftime("%Y-%m"),
    )


async def send_monthly_occasion_summaries(
    now: datetime | None = None,
) -> None:
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    async with async_session() as db:
        user_rows = await db.execute(
            select(User)
            .join(Occasion, Occasion.user_id == User.id)
            .where(Occasion.deleted_at.is_(None))
            .distinct()
        )
        users = user_rows.scalars().all()
        user_context = {
            user.id: (user, context)
            for user in users
            if (context := _local_month_context(user, now_utc)) is not None
        }
        if not user_context:
            return

        rows = await db.execute(
            select(Occasion)
            .join(User, User.id == Occasion.user_id)
            .where(
                Occasion.user_id.in_(user_context),
                Occasion.deleted_at.is_(None),
            )
        )

        by_user: dict[str, list[tuple[date, Occasion]]] = {}
        contact_ids: set[str] = set()
        for occ in rows.scalars().all():
            _, (today_local, _, _) = user_context[occ.user_id]
            occurrence = next_occurrence(
                occ,
                now_utc,
                reference_date=today_local,
            )
            occurrence_date = occasion_calendar_date(occurrence)
            if occurrence_date < today_local:
                continue
            if (occurrence_date.year, occurrence_date.month) != (
                today_local.year,
                today_local.month,
            ):
                continue
            if occ.skipped_year == str(occurrence_date.year):
                continue
            if occ.snooze_until and occ.snooze_until > now_utc:
                continue
            by_user.setdefault(occ.user_id, []).append((occurrence_date, occ))
            if occ.contact_id:
                contact_ids.add(occ.contact_id)

        if not by_user:
            return

        contact_names: dict[str, str] = {}
        if contact_ids:
            contact_rows = await db.execute(
                select(Contact.id, Contact.name).where(Contact.id.in_(contact_ids))
            )
            contact_names = {cid: name for cid, name in contact_rows.all()}

    r = get_redis()
    sent = 0
    sent_months: set[str] = set()
    for user_id, unsorted_occasions in by_user.items():
        user, (_, month_label, month_marker) = user_context[user_id]
        occasions = sorted(unsorted_occasions, key=lambda pair: pair[0])
        next_date_local, next_occ = occasions[0]

        if r is not None:
            try:
                fresh = await r.set(
                    f"wk:monthly:{user.phone}:{month_marker}",
                    "1", nx=True, ex=40 * 86400,
                )
                if not fresh:
                    continue  # Already summarised this month.
            except Exception as e:
                logger.warning("monthly summary dedupe Redis error: %s", e)

        label = _occasion_label(next_occ)
        contact_name = contact_names.get(next_occ.contact_id or "")
        next_phrase = f"{contact_name}'s {label}" if contact_name else f"your {label}"
        next_date_label = next_date_local.strftime("%d %b")
        count = len(occasions)
        count_word = "occasion" if count == 1 else "occasions"

        text = (
            f"📅 Hi {_first_name(user.name)}, you have {count} {count_word} "
            f"saved on Wishkr for {month_label}.\n\n"
            f"Next up: *{next_phrase}* on {next_date_label}.\n\n"
            f"Reply *LIST* to view all, or *SKIP* to pause reminders for this month."
        )
        try:
            await send_text_or_template(
                user.phone,
                text,
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_MONTHLY_OCCASION_SUMMARY,
                body_params=monthly_summary_params(
                    user_name=user.name,
                    occasion_count=count,
                    month_label=month_label,
                    next_occasion_phrase=next_phrase,
                    next_date_label=next_date_label,
                ),
            )
            sent += 1
            sent_months.add(month_marker)
        except Exception:
            logger.exception(
                "monthly summary send failed user=%s", mask_phone(user.phone)
            )

    logger.info(
        "Monthly occasion summaries sent: %d (local_months=%s)",
        sent,
        ",".join(sorted(sent_months)) or "none",
    )
