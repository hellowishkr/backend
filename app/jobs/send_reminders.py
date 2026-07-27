"""Cron job: send due reminders via WhatsApp.

Throughput model
----------------
Each tick fetches up to ``REMINDER_BATCH_SIZE`` PENDING reminders and dispatches
them in parallel, gated by a semaphore (``REMINDER_CONCURRENCY``) to avoid
flooding Meta's send endpoint or starving the DB pool.

Two distinct knobs control flow:

* ``REMINDER_BATCH_SIZE`` (default 300): how many rows we *claim* per tick.
* ``REMINDER_CONCURRENCY`` (default 15): how many we *send* simultaneously.

Sending is sequential per row but the loop is concurrent — at concurrency=15
and ~300 ms per Meta call, sustained throughput is ~50 sends/sec, well below
both the Meta cloud-API ceiling and the WhatsApp business-initiated tier
limits at any cohort size we plan to support before Q4-2026.

Each dispatch runs in its own DB session so a slow row never holds the cron's
shared session open. The status transition (PENDING → IN_PROGRESS → SENT) is
unchanged — it's the source of truth for crash recovery (see ``_reap_stale``).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings

# The scheduler worker has an India operational window. Recipient-facing
# calendar and quiet-hours decisions use each recipient's saved timezone.
_IST = ZoneInfo("Asia/Kolkata")

from app.db.session import async_session
from app.models import (
    Reminder, ReminderStatus, Occasion, Contact, User,
    CelebrantOccasion, ConversationState, ConversationFlow,
)
from app.models.celebrant_friend import CelebrantFriend
from app.bot import messages as msg
from app.services.whatsapp import is_in_cs_window, send_text_or_template, whatsapp_service
from app.bot.whatsapp_templates import (
    TEMPLATE_FRIEND_OCCASION_REMINDER,
    TEMPLATE_OCCASION_TODAY_UPDATE,
    _first_name,
    days_phrase,
    format_celebrant_greeting,
    friend_reminder_params,
)
from app.services.reminder import CELEBRANT_GREETING_TYPE
from app.utils.timezones import (
    DEFAULT_TIMEZONE_NAME,
    occasion_calendar_date,
    today_for_user,
    valid_timezone_name,
)
from app.utils.observability import mask_phone

logger = logging.getLogger("wishkr.jobs.reminders")


# Transient-failure retries. WhatsApp/Meta blips used to flip a reminder to
# FAILED on the first error and bury it permanently. We now reschedule
# PENDING with exponential-ish backoff up to this many attempts before
# giving up (which itself is loud — see ``_mark_failed``).
_MAX_REMINDER_ATTEMPTS = 4
_REMINDER_RETRY_BACKOFF_MINUTES = (5, 15, 60)  # length must be _MAX_REMINDER_ATTEMPTS - 1


def _minutes_since_midnight(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _inside_window(value: datetime, *, start_minutes: int, end_minutes: int) -> bool:
    """Return whether ``value`` is inside a half-open wall-clock window."""
    now_minutes = _minutes_since_midnight(value)
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _inside_india_operational_window(now: datetime) -> bool:
    """Global worker availability gate; this is not recipient quiet hours."""
    return _inside_window(
        now.astimezone(_IST),
        start_minutes=(
            settings.reminder_window_start_hour_ist * 60
            + settings.reminder_window_start_minute_ist
        ),
        end_minutes=settings.reminder_window_end_hour_ist * 60,
    )


def _recipient_window_is_open(
    now: datetime,
    timezone_name: str | None,
) -> bool:
    """Return whether the actual recipient is inside their local window."""
    recipient_tz = ZoneInfo(
        valid_timezone_name(timezone_name) or DEFAULT_TIMEZONE_NAME
    )
    local_now = now.astimezone(recipient_tz)
    start_minutes = (
        settings.reminder_recipient_window_start_hour * 60
        + settings.reminder_recipient_window_start_minute
    )
    end_minutes = settings.reminder_recipient_window_end_hour * 60
    if _inside_window(
        local_now,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
    ):
        return True
    return False


def _reminder_dispatch_rows():
    """Base query carrying the actual recipient timezone for each reminder."""
    owner = aliased(User, name="reminder_owner")
    target = aliased(User, name="reminder_target")
    recipient_timezone = case(
        (Reminder.target_phone.is_not(None), target.timezone),
        else_=owner.timezone,
    ).label("recipient_timezone")
    return (
        select(
            Reminder,
            recipient_timezone,
        )
        .outerjoin(Occasion, Occasion.id == Reminder.occasion_id)
        .outerjoin(owner, owner.id == Occasion.user_id)
        .outerjoin(
            target,
            and_(
                Reminder.target_phone.is_not(None),
                target.phone == Reminder.target_phone,
            ),
        )
    )


async def _eligible_due_reminder_ids(
    db: AsyncSession,
    now: datetime,
) -> list[str]:
    """Collect a fair due batch filtered by actual-recipient local time.

    The timezone predicate is applied before ORDER BY/LIMIT, so closed
    timezones cannot fill the batch and starve open recipients behind them.
    Rows excluded here remain untouched and PENDING.
    """
    batch_size = max(1, settings.reminder_batch_size)
    due_rows = (
        _reminder_dispatch_rows()
        .where(
            Reminder.status == ReminderStatus.PENDING,
            Reminder.scheduled_at <= now,
        )
        .subquery()
    )
    timezone_values = (
        await db.execute(select(due_rows.c.recipient_timezone).distinct())
    ).scalars().all()
    open_timezone_values = [
        timezone_name
        for timezone_name in timezone_values
        if timezone_name is not None
        and _recipient_window_is_open(now, timezone_name)
    ]
    default_is_open = _recipient_window_is_open(now, None)
    if not open_timezone_values and not default_is_open:
        return []

    timezone_predicates = []
    if open_timezone_values:
        timezone_predicates.append(
            due_rows.c.recipient_timezone.in_(open_timezone_values)
        )
    if default_is_open:
        timezone_predicates.append(due_rows.c.recipient_timezone.is_(None))

    result = await db.execute(
        select(due_rows.c.id)
        .where(or_(*timezone_predicates))
        .order_by(due_rows.c.scheduled_at.asc(), due_rows.c.id.asc())
        .limit(batch_size)
    )
    return list(result.scalars().all())


async def send_due_reminders():
    """Find all PENDING reminders due now and dispatch them concurrently.

    The cron tick does three things in order:

    1. Reap stale IN_PROGRESS rows from crashed dispatches.
    2. Resolve due recipient timezones and fetch an eligible PENDING batch.
    3. Dispatch them in parallel, gated by ``REMINDER_CONCURRENCY``.

    Each individual dispatch opens its own DB session — this is critical for
    concurrency, because the shared session is single-cursor and would
    serialise all DB work. With per-row sessions, ``concurrency=15``
    actually runs 15 independent DB conversations against the pool.
    """
    # --- Phase 1: reap stale claims (uses one shared session) -----------
    async with async_session() as db:
        now = datetime.now(timezone.utc)

        # Reaper: any row that's been IN_PROGRESS for more than 15 min has
        # crashed mid-dispatch (worker restart, OOM, network drop). The
        # schema has no claim timestamp, so we use scheduled_at as a proxy
        # — anything with scheduled_at older than 15 min couldn't legitimately
        # still be in flight. Reset to PENDING so this tick picks it up.
        # Without this, crashed dispatches sit forever (incident 2026-04-30:
        # rem_1776832034_gyh5/w2q3 stuck for 5 days before manual cleanup).
        stale_cutoff = now - timedelta(minutes=15)
        reaped = await db.execute(
            update(Reminder)
            .where(
                Reminder.status == ReminderStatus.IN_PROGRESS,
                Reminder.scheduled_at < stale_cutoff,
            )
            .values(status=ReminderStatus.PENDING)
        )
        if reaped.rowcount:
            logger.warning("Reaped %d stale IN_PROGRESS reminders → PENDING", reaped.rowcount)
            await db.commit()

        # India operational gate. This intentionally remains tick-wide and
        # must not be confused with recipient-local quiet hours: those are
        # evaluated per row below using the phone that will actually receive
        # the message. The reaper above still runs outside this window.
        now_ist = now.astimezone(_IST)
        if not _inside_india_operational_window(now):
            logger.debug(
                "Reminder tick skipped (outside India operational window "
                "%02d:%02d–%02d:00 IST, now=%s IST)",
                settings.reminder_window_start_hour_ist,
                settings.reminder_window_start_minute_ist,
                settings.reminder_window_end_hour_ist,
                now_ist.strftime("%H:%M"),
            )
            return

        # --- Phase 2: fetch a recipient-local eligible batch ----------------
        # The timezone predicate runs before LIMIT, so out-of-window rows
        # remain PENDING without consuming batch slots or starving eligible
        # recipients behind them.
        reminder_ids = await _eligible_due_reminder_ids(db, now)

    if not reminder_ids:
        return

    logger.info(
        "Reminder tick: claiming %d due reminders (batch=%d, concurrency=%d)",
        len(reminder_ids),
        settings.reminder_batch_size,
        settings.reminder_concurrency,
    )

    # --- Phase 3: dispatch concurrently with semaphore-bounded parallelism --
    sem = asyncio.Semaphore(settings.reminder_concurrency)
    results = await asyncio.gather(
        *(_dispatch_one(rid, sem) for rid in reminder_ids),
        return_exceptions=True,
    )

    sent = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if isinstance(r, BaseException))
    skipped = len(results) - sent - failed
    logger.info(
        "Reminder tick done: sent=%d skipped=%d failed=%d total=%d",
        sent, skipped, failed, len(results),
    )


async def _dispatch_one(reminder_id: str, sem: asyncio.Semaphore) -> bool:
    """Dispatch a single reminder in its own DB session under the semaphore.

    Returns ``True`` on a real send, ``False`` on a graceful skip
    (cancelled/duplicate/rate-limited), or raises on failure. The cron
    aggregates these for the tick-level log line.
    """
    async with sem:
        async with async_session() as db:
            # Re-read inside the session — the row may have been claimed by
            # a parallel sibling already (rare; the limit query is ordered,
            # but two ticks racing on overrun is possible if max_instances
            # ever drifts).
            res = await db.execute(
                _reminder_dispatch_rows().where(Reminder.id == reminder_id)
            )
            row = res.one_or_none()
            if row is None:
                return False
            reminder, recipient_timezone = row
            if reminder.status != ReminderStatus.PENDING:
                return False

            recipient_timezone = (
                valid_timezone_name(recipient_timezone)
                or DEFAULT_TIMEZONE_NAME
            )
            is_open = _recipient_window_is_open(
                datetime.now(timezone.utc),
                recipient_timezone,
            )
            if not is_open:
                # Do not claim an out-of-window row. It stays PENDING and is
                # filtered out before LIMIT on later ticks.
                logger.info(
                    "Reminder %s skipped before claim (recipient timezone=%s)",
                    reminder.id,
                    recipient_timezone,
                )
                return False
            try:
                outcome = await dispatch_reminder(db, reminder)
                await db.commit()
                return bool(outcome.get("success") and not outcome.get("skipped"))
            except Exception as e:
                logger.error("Failed to send reminder %s: %s", reminder.id, e)
                _mark_for_retry_or_fail(reminder, str(e))
                db.add(reminder)
                await db.commit()
                raise


def _mark_for_retry_or_fail(reminder: Reminder, error: str) -> None:
    """Bump attempt_count; reschedule PENDING with backoff or flip to FAILED.

    Called on any exception from ``dispatch_reminder``. We keep the row
    PENDING so the cron picks it up again at ``scheduled_at`` (which we
    push forward by the backoff). Exhausted retries land in FAILED with
    ``last_error`` populated for forensics.
    """
    reminder.attempt_count = (reminder.attempt_count or 0) + 1
    reminder.last_error = error[:500]
    if reminder.attempt_count >= _MAX_REMINDER_ATTEMPTS:
        reminder.status = ReminderStatus.FAILED
        return
    backoff_idx = min(reminder.attempt_count - 1, len(_REMINDER_RETRY_BACKOFF_MINUTES) - 1)
    backoff_min = _REMINDER_RETRY_BACKOFF_MINUTES[backoff_idx]
    reminder.status = ReminderStatus.PENDING
    reminder.scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_min)


async def dispatch_reminder(db: AsyncSession, reminder: Reminder) -> dict:
    """Send a single reminder now — shared by the cron loop and the
    ``send_reminder_now`` tool in ``functions.py``.

    Transitions ``reminder.status`` PENDING → IN_PROGRESS → SENT / FAILED,
    flushing after each transition so a crash mid-send doesn't leave the
    row stuck in PENDING. Returns a structured result so callers (tools,
    cron) can log / surface the outcome uniformly.
    """
    now = datetime.now(timezone.utc)

    # IN_PROGRESS claim — prevents the cron and a tool call from racing on
    # the same row. Note: the advisory lock in agent.py serializes the tool
    # path per-phone; the status transition guards against the cron.
    reminder.status = ReminderStatus.IN_PROGRESS
    db.add(reminder)
    await db.flush()

    occ_result = await db.execute(select(Occasion).where(Occasion.id == reminder.occasion_id))
    occasion = occ_result.scalar_one_or_none()
    if not occasion:
        reminder.status = ReminderStatus.FAILED
        db.add(reminder)
        await db.flush()
        return {"success": False, "error": "occasion_missing"}

    user_result = await db.execute(select(User).where(User.id == occasion.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        reminder.status = ReminderStatus.FAILED
        db.add(reminder)
        await db.flush()
        return {"success": False, "error": "user_missing"}

    occasion_label = occasion.custom_label or occasion.type.value.replace("_", " ").title()
    # Use the occasion owner's calendar when describing how many days remain.
    days_until = (
        occasion_calendar_date(occasion.date)
        - today_for_user(user, now)
    ).days

    if reminder.target_phone:
        send_resp = await _send_friend_reminder(db, reminder, occasion, user, occasion_label, days_until)
        sent_to = reminder.target_phone
    elif reminder.message_type == CELEBRANT_GREETING_TYPE:
        # Day-of "happy birthday/anniversary/…" to the celebrant themselves.
        send_resp = await _send_celebrant_greeting(db, occasion, user, occasion_label)
        sent_to = user.phone
    else:
        send_resp = await _send_user_reminder(db, reminder, occasion, user, occasion_label, days_until)
        sent_to = user.phone

    if reminder.status in (ReminderStatus.CANCELLED, ReminderStatus.FAILED):
        db.add(reminder)
        await db.flush()
        success = reminder.status == ReminderStatus.CANCELLED
        return {
            "success": success,
            "skipped": success,
            "reminder_id": reminder.id,
            "sent_to": sent_to,
            "message_type": reminder.message_type,
            "reason": reminder.last_error,
        }

    # Persist the Meta-returned wamid for delivery-status correlation. We log
    # it before flushing so a DB error doesn't lose the audit trail.
    wamid = _extract_wamid(send_resp)
    if wamid:
        reminder.whatsapp_msg_id = wamid
    reminder.status = ReminderStatus.SENT
    reminder.sent_at = datetime.now(timezone.utc)
    db.add(reminder)
    await db.flush()

    return {
        "success": True,
        "reminder_id": reminder.id,
        "sent_to": sent_to,
        "sent_at": reminder.sent_at.isoformat(),
        "message_type": reminder.message_type,
        "whatsapp_msg_id": wamid,
    }


def _extract_wamid(resp: dict | None) -> str | None:
    """Pull Meta's returned wamid out of a send_text / send_template response.

    Returns None on absence so callers can still record the send happened
    even if the id shape changes upstream — we'd rather lose correlation
    than fail the reminder.
    """
    if not isinstance(resp, dict):
        return None
    messages = resp.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")


def _user_reminder_subject(
    occasion: Occasion,
    user: User,
    contact: Contact | None,
    occasion_label: str,
) -> str:
    """Render the subject line for reminders sent to the account owner."""
    if contact:
        return f"{contact.name}'s {occasion_label}"
    return f"your {occasion_label}"


def _friend_visible_celebrant_name(celebrant: User) -> str:
    """Name shown to friends; includes a masked phone suffix to disambiguate."""
    name = (celebrant.name or "Your friend").strip() or "Your friend"
    digits = "".join(ch for ch in (celebrant.phone or "") if ch.isdigit())
    if len(digits) >= 4:
        return f"{name} (phone ending {digits[-4:]})"
    return name


async def _send_user_reminder(
    db: AsyncSession, reminder: Reminder, occasion: Occasion,
    user: User, occasion_label: str, days_until: int,
) -> dict | None:
    """Original flow: remind the user about someone else's occasion.

    Returns the raw Meta API response so the caller can capture the wamid.
    """
    contact = None
    if occasion.contact_id:
        contact_result = await db.execute(select(Contact).where(Contact.id == occasion.contact_id))
        contact = contact_result.scalar_one_or_none()

    subject = _user_reminder_subject(occasion, user, contact, occasion_label)

    if days_until == 0:
        message = msg.REMINDER_TODAY.format(subject=subject)
    else:
        suggestions = (
            "🎧 Something for their hobbies\n"
            "🍫 A thoughtful treat or hamper\n"
            "📝 A personal note with a useful gift"
        )
        message = msg.REMINDER.format(
            subject=subject,
            days=days_until,
            date=occasion.date.strftime("%d %b"),
            suggestions=suggestions,
        )

    # Business-initiated messages must use a Meta-approved template unless
    # the user's 24h customer-service window is currently open. Outside that
    # window, Meta accepts ``send_text`` with HTTP 200 (and returns a wamid)
    # but silently drops the message — every reminder for occasion
    # occ_1779712821 was marked SENT yet never delivered (June 2026). Same
    # trap as the friend path (incident 2026-04-29); same fix: template
    # first, free-text fallback only when the CS window is verifiably open.
    if is_in_cs_window(user.last_inbound_at):
        resp = await whatsapp_service.send_text(user.phone, message)
        logger.info("User reminder sent (free text, CS window open): user=%s, occasion=%s",
                    mask_phone(user.phone), occasion.id)
        return resp

    # {{2}} of the approved template is the celebrant/subject name. With no
    # contact (the user's own occasion), use the user's name so it renders
    # "…Karan's Birthday is coming up…".
    subject_name = contact.name if contact else (user.name or "Your")
    resp = await whatsapp_service.send_template(
        user.phone,
        template_name=TEMPLATE_FRIEND_OCCASION_REMINDER,
        body_params=[
            _first_name(user.name),
            subject_name,
            occasion_label,
            days_phrase(days_until),
        ],
    )
    logger.info("User reminder sent (template, CS window closed): user=%s, occasion=%s",
                mask_phone(user.phone), occasion.id)
    return resp


async def _send_celebrant_greeting(
    db: AsyncSession, occasion: Occasion, user: User, occasion_label: str,
) -> dict | None:
    """Day-of greeting sent to the celebrant about their OWN occasion.

    Warm free-text greeting inside the celebrant's CS window; outside it,
    the ``occasion_today_update`` Utility template (a status update rather
    than a greeting — Meta classifies greetings as Marketing) so the day-of
    message is never silently dropped.
    """
    body = format_celebrant_greeting(
        occasion_type=occasion.type,
        custom_label=occasion.custom_label,
        celebrant_name=user.name,
    )

    resp = await send_text_or_template(
        user.phone,
        body,
        last_inbound_at=user.last_inbound_at,
        template_name=TEMPLATE_OCCASION_TODAY_UPDATE,
        body_params=[_first_name(user.name), occasion_label],
    )
    logger.info("Celebrant greeting sent: user=%s, occasion=%s",
                mask_phone(user.phone), occasion.id)
    return resp


async def _send_friend_reminder(
    db: AsyncSession, reminder: Reminder, occasion: Occasion,
    celebrant: User, occasion_label: str, days_until: int,
) -> dict | None:
    """Celebrant flow: remind the friend about the celebrant's occasion.

    Returns the Meta API response from whichever path (template or text)
    succeeded, so the caller can capture the wamid.
    """
    friend_phone = reminder.target_phone
    if not friend_phone:
        # Friend reminders are always targeted; a NULL target_phone indicates
        # a bad row or a caller routing the wrong reminder type here.
        raise RuntimeError(f"friend_reminder_missing_target_phone reminder={reminder.id}")
    send_resp: dict | None = None

    # Get celebrant preferences from CelebrantOccasion
    cel_result = await db.execute(
        select(CelebrantOccasion).where(CelebrantOccasion.occasion_id == occasion.id)
    )
    cel_occ = cel_result.scalar_one_or_none()

    if not cel_occ:
        logger.warning(
            "Marking malformed friend reminder %s failed: CelebrantOccasion not found for occasion %s",
            reminder.id,
            reminder.occasion_id,
        )
        reminder.status = ReminderStatus.FAILED
        reminder.last_error = (
            f"celebrant_occasion_missing reminder={reminder.id} "
            f"occasion={reminder.occasion_id}"
        )
        db.add(reminder)
        await db.flush()
        return None

    # Look up the CelebrantFriend row for this (celebrant_occasion, phone).
    # We need it for the recipient name ({{1}} of the approved template) and
    # for the dedupe guard below — `reminded_at` is the source of truth for
    # "have we already nudged this friend about this occasion this cycle?".
    cf_result = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == friend_phone,
        )
    )
    cel_friend = cf_result.scalar_one_or_none()

    # Dedupe: skip if this friend was already reminded for this occasion in
    # the last 30 days. Guards against duplicate Reminder rows scheduled by
    # tools/cron, and against retries firing after a successful send. The
    # 30-day window is tight enough that next year's birthday will fire
    # cleanly, but wide enough to cover same-cycle re-runs.
    if cel_friend and cel_friend.reminded_at:
        age = datetime.now(timezone.utc) - cel_friend.reminded_at
        if age < timedelta(days=30):
            logger.info(
                "Skipping friend reminder (already reminded %s ago): friend=%s, occasion=%s",
                age, mask_phone(friend_phone), occasion.id,
            )
            reminder.status = ReminderStatus.CANCELLED
            reminder.last_error = "duplicate_skip: friend reminded within last 30d"
            db.add(reminder)
            await db.flush()
            return None

    # Cross-celebrant rate limit: at most one friend-reminder template per
    # ``target_phone`` per 24h across the whole platform. If two unrelated
    # celebrants both have a friend in this phone (e.g. Karan and Sahil
    # both list ***5424 and have occasions on the same day), only the
    # first nudge fires. The rest are CANCELLED here without stamping
    # ``reminded_at`` on their CelebrantFriend, so they re-enter the
    # queue tomorrow when the cron picks up the next-cycle row.
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    other_recent_q = (
        select(Reminder.id)
        .where(
            Reminder.target_phone == friend_phone,
            Reminder.status == ReminderStatus.SENT,
            Reminder.sent_at >= cutoff_24h,
            Reminder.id != reminder.id,
        )
        .limit(1)
    )
    other_recent = (await db.execute(other_recent_q)).scalar_one_or_none()
    if other_recent:
        logger.info(
            "Skipping friend reminder (rate-limited — %s already received a reminder today via %s): friend=%s, occasion=%s",
            mask_phone(friend_phone), other_recent, mask_phone(friend_phone), occasion.id,
        )
        reminder.status = ReminderStatus.CANCELLED
        reminder.last_error = (
            "rate_limited: friend already received a reminder in last 24h"
        )
        db.add(reminder)
        await db.flush()
        return None

    recipient_name = cel_friend.name if cel_friend else None

    prefs = cel_occ.preferences_list
    # Used only by the free-text fallback path (CS window open) — the
    # approved Utility template does not include preferences as a variable.
    prefs_formatted = ", ".join(prefs) if prefs else "anything thoughtful"
    # `budget_label` is not a persisted field on CelebrantOccasion (we don't
    # capture per-occasion budgets from the celebrant). Use a neutral default
    # so the friend-reminder template still renders a sensible string. If a
    # per-occasion budget is added later, read it via getattr for forward
    # compatibility.
    budget = getattr(cel_occ, "budget_label", None) or "Any budget"
    celebrant_name = _friend_visible_celebrant_name(celebrant)

    if days_until == 0:
        message = msg.FRIEND_REMINDER_TODAY.format(
            celebrant_name=celebrant_name,
            occasion=occasion_label,
            preferences=prefs_formatted,
            budget=budget,
        )
    else:
        message = msg.FRIEND_REMINDER.format(
            celebrant_name=celebrant_name,
            occasion=occasion_label,
            days=days_until,
            date=occasion.date.strftime("%d %b"),
            preferences=prefs_formatted,
            budget=budget,
        )

    # Business-initiated messages must use a Meta-approved template unless
    # the friend's 24h customer-service window is currently open. Outside
    # that window, Meta accepts ``send_text`` with HTTP 200 (and returns a
    # wamid) but silently drops the message — so a 200 here is NOT proof
    # of delivery, and falling back unconditionally produced lying
    # "reminder sent" replies (incident 2026-04-29).
    try:
        send_resp = await whatsapp_service.send_template(
            friend_phone,
            template_name=TEMPLATE_FRIEND_OCCASION_REMINDER,
            # Per-occasion phrasing + days/today wording lives in
            # app.bot.whatsapp_templates so adding a new occasion type
            # only needs a constant entry, not a dispatcher edit.
            body_params=friend_reminder_params(
                recipient_name=recipient_name,
                occasion_type=occasion.type,
                custom_label=occasion.custom_label,
                celebrant_name=celebrant_name,
                days_until=days_until,
            ),
        )
    except Exception as template_err:
        # Only attempt the free-text fallback if the friend has actually
        # messaged us in the last 24h. We look up an existing User row by
        # phone — if there's no row, or last_inbound_at is stale/None, the
        # friend is outside the CS window and send_text will be dropped.
        friend_user_res = await db.execute(
            select(User).where(User.phone == friend_phone)
        )
        friend_user = friend_user_res.scalar_one_or_none()
        cs_open = bool(friend_user and is_in_cs_window(friend_user.last_inbound_at))
        if not cs_open:
            logger.warning(
                "Template send failed for %s and CS window closed — refusing send_text fallback: %s",
                mask_phone(friend_phone), template_err,
            )
            raise RuntimeError(
                f"friend_reminder template send failed and CS window closed: "
                f"template={template_err!s}"
            ) from template_err

        logger.warning(
            "Template send failed for %s, attempting send_text fallback (CS window open): %s",
            mask_phone(friend_phone), template_err,
        )
        try:
            send_resp = await whatsapp_service.send_text(friend_phone, message)
        except Exception as text_err:
            # Both paths failed — let the caller mark the row for retry/fail.
            raise RuntimeError(
                f"friend_reminder both template and send_text failed: "
                f"template={template_err!s} text={text_err!s}"
            ) from text_err

    # Set the friend's conversation state so replies stay attached to the
    # friend occasion flow.
    state_result = await db.execute(
        select(ConversationState).where(ConversationState.phone == friend_phone)
    )
    state = state_result.scalar_one_or_none()

    if not state:
        state = ConversationState(
            phone=friend_phone,
            flow=ConversationFlow.IDLE,
            step=None,
            temp_data=json.dumps({"celebrant_occasion_id": cel_occ.id}),
            last_activity=datetime.now(timezone.utc),
        )
        db.add(state)
    else:
        # Preserve existing temp_data but add celebrant context
        existing = json.loads(state.temp_data or "{}")
        existing["celebrant_occasion_id"] = cel_occ.id
        state.temp_data = json.dumps(existing)
        db.add(state)

    # Stamp the dedupe marker now that the send (template or fallback)
    # succeeded. This is what the guard at the top of the function reads
    # next time, so it must happen on every successful send path.
    if cel_friend:
        cel_friend.reminded_at = datetime.now(timezone.utc)
        db.add(cel_friend)

    await db.flush()
    logger.info("Friend reminder sent: friend=%s, celebrant=%s, occasion=%s",
                mask_phone(friend_phone), celebrant.name, occasion.id)
    return send_resp
