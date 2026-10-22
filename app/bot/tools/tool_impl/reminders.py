import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Contact, Occasion,
    CelebrantOccasion, CelebrantFriend,
    Reminder, ReminderStatus,
)
from app.utils.dates import next_occurrence
from app.utils.id_gen import generate_id
from app.utils.timezones import (
    local_schedule_utc,
    occasion_calendar_date,
    today_for_user,
    timezone_for_user,
)
from app.services.reminder import _MESSAGE_TYPE_BY_DAYS
from app.bot.tools.validators import normalize_phone
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_REGISTER, _HINT_OCCASION_UNKNOWN,
    _get_user, _user_owns_occasion,
)

import logging
logger = logging.getLogger("wishkr.tools.functions")


def _format_reminder_row(rem: Reminder, target_name: str | None = None) -> dict:
    """Render a Reminder row for the agent.

    ``target_name`` is joined in by the caller (see ``_build_target_name_map``)
    so the agent references reminders by person, not by phone number.
    "Reminded Alisha on Apr 23" reads better than the raw phone and
    eliminates the "guess who this number is" hallucination vector.

    ``whatsapp_msg_id`` is the Meta Cloud API wamid populated at dispatch
    time. Lets the agent say "delivered" with audit context, and keeps
    the door open for a future delivery-status correlation job that reads
    Meta's webhook callbacks back onto the same row.
    """
    return {
        "reminder_id": rem.id,
        "scheduled_at": rem.scheduled_at.isoformat() if rem.scheduled_at else None,
        "sent_at": rem.sent_at.isoformat() if rem.sent_at else None,
        "status": rem.status.value,
        "message_type": rem.message_type,
        "target_phone": rem.target_phone,
        "target_name": target_name,
        "whatsapp_msg_id": rem.whatsapp_msg_id,
    }


async def _build_target_name_map(
    db: AsyncSession, occasion: Occasion,
) -> dict[str | None, str]:
    """Build a phone → friendly-name map for this occasion's reminders.

    Key None maps to the owner-stream reminder context (``target_phone IS NULL``).
    Those reminders tell the USER about someone else's occasion, so the
    friendly name is the Contact linked to the occasion — not the user.

    Non-None keys are friend phones from the celebrant flow (the user's
    OWN occasion, reminders going out to friends), sourced from
    CelebrantFriend.name.
    """
    name_map: dict[str | None, str] = {}

    # Owner stream: the Contact the occasion is ABOUT.
    if occasion.contact_id:
        contact = await db.get(Contact, occasion.contact_id)
        if contact and contact.name:
            name_map[None] = contact.name
    name_map.setdefault(None, "You")

    # Celebrant stream: each friend by phone.
    cel_q = await db.execute(
        select(CelebrantOccasion).where(CelebrantOccasion.occasion_id == occasion.id)
    )
    cel_occ = cel_q.scalar_one_or_none()
    if cel_occ:
        friends_q = await db.execute(
            select(CelebrantFriend).where(CelebrantFriend.celebrant_occasion_id == cel_occ.id)
        )
        for f in friends_q.scalars().all():
            if f.phone and f.name:
                name_map[f.phone] = f.name
    return name_map


async def get_reminder_schedule(db: AsyncSession, phone: str, args: dict) -> dict:
    """Return every reminder row for an occasion the user owns.

    Grouped by status so the agent can render it naturally:
      "You have 2 reminders pending (Apr 28, Apr 30) and 1 already sent
      on Apr 23."
    """
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id:
        return {"error": "occasion_id is required"}

    occ = await _user_owns_occasion(db, occasion_id, user.id)
    if occ is None:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    res = await db.execute(
        select(Reminder)
        .where(Reminder.occasion_id == occasion_id)
        .order_by(Reminder.scheduled_at)
    )
    reminders = res.scalars().all()

    name_map = await _build_target_name_map(db, occ)
    rows = [_format_reminder_row(r, name_map.get(r.target_phone)) for r in reminders]

    occasion_label = occ.custom_label or (occ.type.value.replace("_", " ").title() if occ.type else "Occasion")
    days_until = (
        (
            occasion_calendar_date(occ.date) - today_for_user(user)
        ).days
        if occ.date
        else None
    )

    return {
        "occasion_id": occasion_id,
        "occasion": occasion_label,
        "date": occ.date.strftime("%d %B %Y") if occ.date else None,
        "days_until": days_until,
        "reminders": rows,
        "pending_count": sum(1 for r in rows if r["status"] == ReminderStatus.PENDING.value),
        "sent_count": sum(1 for r in rows if r["status"] == ReminderStatus.SENT.value),
    }


async def get_reminder_history(db: AsyncSession, phone: str, args: dict) -> dict:
    """Same data as get_reminder_schedule but partitioned {pending, sent, other}.

    The agent calls this when the user asks "when did you last inform X?"
    or "what reminders have gone out?". Keeping it a separate tool makes the
    natural-language intent unambiguous at the routing layer.
    """
    sched = await get_reminder_schedule(db, phone, args)
    if sched.get("error"):
        return sched

    pending, sent, other = [], [], []
    for r in sched.get("reminders", []):
        if r["status"] == ReminderStatus.PENDING.value:
            pending.append(r)
        elif r["status"] == ReminderStatus.SENT.value:
            sent.append(r)
        else:
            other.append(r)

    return {
        "occasion_id": sched["occasion_id"],
        "occasion": sched["occasion"],
        "days_until": sched.get("days_until"),
        "pending": pending,
        "sent": sent,
        "other": other,  # FAILED / CANCELLED / IN_PROGRESS — surface so the agent can flag delivery issues
    }


def _message_type_for_days_before(days_before: int) -> str:
    return _MESSAGE_TYPE_BY_DAYS.get(
        days_before,
        f"custom_{days_before}_days_before",
    )


async def set_reminder_schedule(db: AsyncSession, phone: str, args: dict) -> dict:
    """Replace the caller's pending reminders with explicit day offsets.

    Sent/history rows and friend-targeted reminders are preserved. Missed
    offsets are reported instead of being converted into an unsolicited
    immediate reminder.
    """
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id:
        return {"error": "occasion_id is required"}

    occ = await _user_owns_occasion(db, occasion_id, user.id)
    if occ is None:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    raw_days = args.get("days_before")
    if not isinstance(raw_days, list) or not raw_days:
        return {
            "error": "days_before_required",
            "message": "Pass one or more reminder offsets, such as [1, 0].",
        }

    parsed_days: list[int] = []
    for raw in raw_days:
        if isinstance(raw, bool):
            return {"error": "invalid_days_before", "message": "Reminder offsets must be whole numbers."}
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return {"error": "invalid_days_before", "message": "Reminder offsets must be whole numbers."}
        if value < 0 or value > 365:
            return {
                "error": "invalid_days_before",
                "message": "Reminder offsets must be between 0 and 365 days before the occasion.",
            }
        parsed_days.append(value)
    days_before = sorted(set(parsed_days), reverse=True)

    hour = args.get("local_hour", 9)
    minute = args.get("local_minute", 0)
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return {"error": "invalid_local_time", "message": "The hour must be between 0 and 23."}
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
        return {"error": "invalid_local_time", "message": "The minute must be between 0 and 59."}

    now = datetime.now(timezone.utc)
    occasion_date = next_occurrence(
        occ,
        now,
        reference_date=today_for_user(user, now),
    )
    if occasion_date.tzinfo is None:
        occasion_date = occasion_date.replace(tzinfo=timezone.utc)

    created: list[dict] = []
    skipped_past: list[int] = []
    for offset in days_before:
        scheduled = local_schedule_utc(
            occasion_date - timedelta(days=offset),
            user,
            hour=hour,
            minute=minute,
        )
        if scheduled <= now:
            # "On the day" is still useful after the normal 09:00 IST slot:
            # queue it now. Earlier offsets stay skipped instead of turning
            # into surprise immediate messages.
            if (
                offset == 0
                and occasion_calendar_date(occasion_date) == today_for_user(user)
                and "local_hour" not in args and "local_minute" not in args
            ):
                scheduled = now
            else:
                skipped_past.append(offset)
                continue

        created.append({
            "reminder_id": generate_id("rem"),
            "days_before": offset,
            "scheduled_at": scheduled.isoformat(),
            "scheduled_local": scheduled.astimezone(timezone_for_user(user)).isoformat(),
        })

    if not created:
        return {
            "error": "all_requested_times_passed",
            "occasion_id": occasion_id,
            "days_before": days_before,
            "skipped_past": skipped_past,
            "cancelled_pending": 0,
            "message": (
                "All requested reminder times have already passed. Ask whether "
                "the user wants an immediate reminder instead."
            ),
        }

    deleted = await db.execute(
        delete(Reminder).where(
            Reminder.occasion_id == occasion_id,
            Reminder.target_phone.is_(None),
            Reminder.status == ReminderStatus.PENDING,
        )
    )
    for row in created:
        db.add(Reminder(
            id=row["reminder_id"],
            occasion_id=occasion_id,
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            status=ReminderStatus.PENDING,
            message_type=_message_type_for_days_before(row["days_before"]),
            target_phone=None,
        ))
    await db.flush()

    return {
        "success": True,
        "occasion_id": occasion_id,
        "occasion": occ.custom_label or occ.type.value.replace("_", " ").title(),
        "date": occasion_date.strftime("%d %B %Y"),
        "days_before": days_before,
        "scheduled_reminders": created,
        "skipped_past": skipped_past,
        "cancelled_pending": int(deleted.rowcount or 0),
        "message": "Reminder schedule updated.",
    }


# In-process dedup for ad-hoc reminder sends. Prevents a double-tap user or
# a webhook retry from firing the same reminder twice within the bucket.
_AD_HOC_DEDUP: dict[tuple[str, str], float] = {}
_AD_HOC_DEDUP_SECONDS = 60.0


def _ad_hoc_key(occasion_id: str, target_phone: str | None) -> tuple[str, str]:
    return (occasion_id, target_phone or "__owner__")


async def send_reminder_now(db: AsyncSession, phone: str, args: dict) -> dict:
    """Fire a reminder immediately for an occasion the user owns.

    Behaviour:
      1. If target_phone is given, dispatch through the friend-flow
         (requires CelebrantOccasion). Else the occasion-owner stream.
      2. Always creates a fresh ad-hoc PENDING row and dispatches it —
         scheduled reminders are never consumed or rescheduled by an
         ad-hoc send.
      3. Dedupes within a 60 s window per (occasion_id, target_phone) so
         retries and double-taps do not fire duplicate messages.

    Returns a structured result with sent_to / sent_at so the agent can
    confirm to the user naturally.
    """
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = (args.get("occasion_id") or "").strip()
    _raw_target = (args.get("target_phone") or "").strip()
    target_phone = normalize_phone(_raw_target) if _raw_target else None

    if not occasion_id:
        return {"error": "occasion_id is required"}

    occ = await _user_owns_occasion(db, occasion_id, user.id)
    if occ is None:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    # Friend-flow precondition: a target_phone routes dispatch through
    # _send_friend_reminder, which requires a CelebrantOccasion row. Without
    # this check the row is created, retried 4× by the cron, and then lands in
    # FAILED with `celebrant_occasion_missing` (see incident on
    # rem_1777892648_948647eb, 2026-05). Fail fast at creation instead.
    if target_phone is not None:
        cel_occ_exists = await db.execute(
            select(CelebrantOccasion.id).where(
                CelebrantOccasion.occasion_id == occasion_id
            )
        )
        if cel_occ_exists.scalar_one_or_none() is None:
            return _err(
                "celebrant_occasion_missing",
                "This occasion has no celebrant setup, so friend reminders "
                "can't be sent for it. Ask the user to set up the celebrant "
                "occasion first, or send the reminder to themselves "
                "(omit target_phone).",
                occasion_id=occasion_id,
            )

    key = _ad_hoc_key(occasion_id, target_phone)
    now_ts = time.time()
    prev = _AD_HOC_DEDUP.get(key)
    if prev is not None and (now_ts - prev) < _AD_HOC_DEDUP_SECONDS:
        return _err(
            "rate_limited",
            "An immediate reminder for this occasion was already sent moments ago. "
            "Tell the user it's been delivered and avoid re-sending.",
            cooldown_seconds=int(_AD_HOC_DEDUP_SECONDS - (now_ts - prev)),
        )

    now = datetime.now(timezone.utc)

    # Always dispatch a fresh ad-hoc row. Repurposing the nearest PENDING
    # reminder (previous behaviour) silently consumed the user's scheduled
    # reminder: an ad-hoc send on 7 Jul 2026 marked the 10-days-before
    # early_reminder SENT, so nothing fired on its scheduled date. "Remind
    # me now" must never cancel a future scheduled reminder.
    reminder = Reminder(
        id=generate_id("rem"),
        occasion_id=occasion_id,
        scheduled_at=now,
        status=ReminderStatus.PENDING,
        message_type="ad_hoc_reminder",
        target_phone=target_phone,
    )
    db.add(reminder)
    await db.flush()

    # Import locally to avoid a circular (reminders → jobs → functions).
    from app.jobs.send_reminders import dispatch_reminder

    dispatch_result = await dispatch_reminder(db, reminder)
    if not dispatch_result.get("success"):
        return _err(
            "reminder_dispatch_failed",
            "Something went wrong while sending the reminder. Apologise to the user and offer to retry in a minute.",
            detail=dispatch_result.get("error"),
        )

    _AD_HOC_DEDUP[key] = now_ts
    # Opportunistic GC.
    if len(_AD_HOC_DEDUP) > 5_000:
        cutoff = now_ts - (_AD_HOC_DEDUP_SECONDS * 10)
        for k, ts in list(_AD_HOC_DEDUP.items()):
            if ts < cutoff:
                _AD_HOC_DEDUP.pop(k, None)

    return {
        "success": True,
        "occasion_id": occasion_id,
        "sent_to": dispatch_result["sent_to"],
        "sent_at": dispatch_result["sent_at"],
        "message_type": dispatch_result["message_type"],
        "ad_hoc": True,
    }
