import json
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    User, Occasion, OccasionType, default_recurring_for_occasion,
    CelebrantOccasion, CelebrantFriend, Reminder, ReminderStatus,
)
from app.services.reminder import schedule_reminders, schedule_celebrant_greeting
from app.utils.id_gen import generate_id
from app.utils.dedup import get_purchased_categories
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_REGISTER, _HINT_OCCASION_UNKNOWN, _HINT_CELEBRANT_UNKNOWN,
    _get_user, _user_owns_occasion,
)
from app.utils.timezones import occasion_calendar_date, today_for_user
from app.utils.observability import mask_phone
from datetime import datetime, timezone

logger = logging.getLogger("wishkr.tools.functions")


def _clean_friend_phones(
    values: list[str] | None,
    *,
    self_phone: str,
) -> tuple[list[str], dict[str, str]]:
    clean_phones: list[str] = []
    phone_to_name: dict[str, str] = {}
    for raw in values or []:
        fp = str(raw)
        name_part: str | None = None
        if "(" in fp and ")" in fp:
            open_idx = fp.rfind("(")
            close_idx = fp.rfind(")")
            if open_idx < close_idx:
                name_part = fp[:open_idx].strip() or None
                fp = fp[open_idx + 1:close_idx]

        digits = "".join(c for c in fp if c.isdigit())
        if len(digits) == 10:
            digits = "91" + digits
        elif len(digits) == 12 and digits.startswith("91"):
            pass
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "91" + digits[1:]
        else:
            logger.warning(
                "Invalid friend phone format: %s -> %s",
                mask_phone(fp),
                mask_phone(digits),
            )
            continue
        if digits == self_phone:
            continue
        if digits not in clean_phones:
            clean_phones.append(digits)
        if name_part:
            phone_to_name[digits] = name_part
    return clean_phones, phone_to_name


async def setup_celebrant_occasion(db: AsyncSession, phone: str, args: dict) -> dict:
    """Set up the user's OWN occasion so friends get reminded with gift preferences."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_type_str = args.get("occasion_type", "BIRTHDAY")
    date_day = args.get("date_day")
    date_month = args.get("date_month")
    preferences_provided = "preferences" in args
    wishlist_provided = "wishlist_urls" in args
    note_provided = "note_to_friends" in args
    friends_provided = "friend_phones" in args
    preferences = args.get("preferences", [])
    wishlist_urls = args.get("wishlist_urls", [])
    note_to_friends = args.get("note_to_friends")
    friend_phones = args.get("friend_phones", [])

    if not date_day or not date_month:
        return {"error": "Date is required"}

    try:
        occasion_type = OccasionType(occasion_type_str)
    except ValueError:
        return {"error": f"Invalid occasion type: {occasion_type_str}"}
    if occasion_type == OccasionType.CUSTOM and not (args.get("custom_label") or "").strip():
        return {"error": "custom_label is required when occasion_type is CUSTOM"}
    recurring_arg = args.get("recurring")
    recurring = (
        default_recurring_for_occasion(occasion_type)
        if recurring_arg is None
        else bool(recurring_arg)
    )

    # Build date
    today = today_for_user(user)
    date_year = args.get("date_year")
    try:
        year = int(date_year) if date_year is not None else today.year
    except (TypeError, ValueError):
        return {"error": f"Invalid year: {date_year}"}
    if year < 1900 or year > 2200:
        return {"error": f"Invalid year: {year}"}
    try:
        occasion_date = datetime(year, date_month, date_day, tzinfo=timezone.utc)
    except ValueError:
        return {"error": f"Invalid date: {date_day}/{date_month}"}

    if (
        occasion_calendar_date(occasion_date) < today
        and (recurring or date_year is None)
    ):
        occasion_date = datetime(year + 1, date_month, date_day, tzinfo=timezone.utc)
    elif occasion_calendar_date(occasion_date) < today:
        return {
            "error": "date_in_past",
            "message": "A one-time occasion needs a current or future date.",
        }

    # Clean friend phones - handle formats like "919876543210" or "Name (919876543210)"
    clean_phones, phone_to_name = _clean_friend_phones(
        friend_phones,
        self_phone=phone,
    )

    if friends_provided and friend_phones and not clean_phones:
        return {"error": "No valid friend phone numbers provided"}

    # ── Idempotency: reuse an existing self-occasion for the same
    # (user, type, month+day) instead of creating a parallel duplicate.
    # Without this, calling the tool twice (re-confirmation, agent retry,
    # user repeating themselves) produced N×Reminder rows and the friend
    # got bombarded N×times on the day-of (incident 2026-04-30:
    # Karan ended up with 3 birthday occasions). The dispatcher's
    # CelebrantFriend.reminded_at guard catches the spam at send-time,
    # but creating dupes in the first place still pollutes the DB and
    # confuses every downstream tool that lists a user's occasions.
    existing_q = (
        select(Occasion)
        .where(
            Occasion.user_id == user.id,
            Occasion.contact_id.is_(None),
            Occasion.type == occasion_type,
            func.extract("month", Occasion.date) == occasion_date.month,
            func.extract("day", Occasion.date) == occasion_date.day,
        )
        .order_by(Occasion.created_at.desc())
    )
    existing = (await db.execute(existing_q)).scalars().first()

    if existing is not None:
        # Progressive calls that only add preferences/wishlists/friends must
        # preserve the original one-time year and recurrence choice.
        if date_year is None:
            occasion_date = existing.date
        if recurring_arg is None:
            recurring = existing.recurring
        # Refresh date (year may have rolled over to next cycle), preferences,
        # wishlist, and note. Treat this call as an "update my occasion"
        # rather than "create a new occasion."
        existing.date = occasion_date
        existing.recurring = recurring
        existing.custom_label = args.get("custom_label") or existing.custom_label
        # Re-setting up an occasion the owner previously soft-deleted revives it
        # (and its old friend links) instead of leaving a hidden ghost row.
        # delete_occasion() purged every Reminder row for this occasion, so on
        # revival the retained CelebrantFriend rows have NO reminder stream —
        # they must be re-scheduled below, not just the newly-added phones.
        was_revived = existing.deleted_at is not None
        existing.deleted_at = None
        db.add(existing)

        cel_existing_q = select(CelebrantOccasion).where(
            CelebrantOccasion.occasion_id == existing.id
        )
        cel_occ = (await db.execute(cel_existing_q)).scalar_one_or_none()
        if cel_occ is None:
            cel_occ = CelebrantOccasion(
                id=generate_id("cel"), occasion_id=existing.id,
                preferences=json.dumps(preferences),
                wishlist_urls=json.dumps(wishlist_urls) if wishlist_urls else None,
                note_to_friends=note_to_friends,
            )
            db.add(cel_occ)
        else:
            if preferences_provided:
                cel_occ.preferences = json.dumps(preferences)
            if wishlist_provided:
                cel_occ.wishlist_urls = (
                    json.dumps(wishlist_urls) if wishlist_urls else None
                )
            if note_provided:
                cel_occ.note_to_friends = note_to_friends
            db.add(cel_occ)
        await db.flush()

        # Additive merge of friend phones: keep existing CelebrantFriend rows,
        # add only phones that aren't already linked to this celebrant_occasion.
        existing_friends_q = select(CelebrantFriend.phone).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id
        )
        existing_friend_phones = {
            row[0] for row in (await db.execute(existing_friends_q)).all()
        }
        new_phones = [p for p in clean_phones if p not in existing_friend_phones]
        for fp in new_phones:
            friend_user_result = await db.execute(select(User).where(User.phone == fp))
            friend_user = friend_user_result.scalar_one_or_none()
            cf = CelebrantFriend(
                id=generate_id("cfr"),
                celebrant_occasion_id=cel_occ.id,
                phone=fp,
                user_id=friend_user.id if friend_user else None,
                name=(friend_user.name if friend_user else None) or phone_to_name.get(fp),
            )
            db.add(cf)
        await db.flush()

        # Which phones need a reminder stream created now?
        #  - Normal additive merge of a LIVE occasion: only newly-added phones;
        #    existing friends already have their stream from the original setup.
        #  - Revival of a soft-deleted occasion: delete_occasion() purged all
        #    reminders, so every retained existing friend needs re-scheduling
        #    too (schedule_reminders is not idempotent for friend phones, so we
        #    must NOT re-run it for existing friends on the live-merge path).
        if was_revived:
            phones_to_schedule = [None, *sorted(existing_friend_phones), *new_phones]
        else:
            phones_to_schedule = new_phones

        if phones_to_schedule:
            sched = await schedule_reminders(
                db, existing, target_phones=phones_to_schedule,
            )
        else:
            sched = {"reminders_created": 0, "tight_window": False,
                     "days_until": (
                         occasion_calendar_date(occasion_date)
                         - today_for_user(user)
                     ).days,
                     "skipped_past": 0}

        # Idempotent — no-op if a greeting was already scheduled for this
        # occasion in the original setup call.
        await schedule_celebrant_greeting(db, existing)

        occasion = existing
        merged = True
    else:
        # No existing self-occasion — fresh create path.
        occasion = Occasion(
            id=generate_id("occ"), user_id=user.id, contact_id=None,
            type=occasion_type, custom_label=args.get("custom_label"),
            date=occasion_date, recurring=recurring,
        )
        db.add(occasion)
        await db.flush()

        cel_occ = CelebrantOccasion(
            id=generate_id("cel"), occasion_id=occasion.id,
            preferences=json.dumps(preferences),
            wishlist_urls=json.dumps(wishlist_urls) if wishlist_urls else None,
            note_to_friends=note_to_friends,
        )
        db.add(cel_occ)
        await db.flush()

        for fp in clean_phones:
            friend_user_res = await db.execute(select(User).where(User.phone == fp))
            friend_user = friend_user_res.scalar_one_or_none()
            cf = CelebrantFriend(
                id=generate_id("cfr"),
                celebrant_occasion_id=cel_occ.id,
                phone=fp,
                user_id=friend_user.id if friend_user else None,
                name=(friend_user.name if friend_user else None) or phone_to_name.get(fp),
            )
            db.add(cf)
        await db.flush()

        # Include the celebrant's own self-reminder stream alongside the
        # friend reminders. Without `None` here, ``schedule_reminders`` would
        # create rows for friends only and the celebrant would never be
        # nudged about their own upcoming occasion (incident 2026-04-30:
        # Karan's birthday — friends got reminded, owner did not).
        sched = await schedule_reminders(
            db, occasion, target_phones=[None, *clean_phones],
        )
        # Day-of "happy birthday/anniversary/…" greeting to the celebrant.
        await schedule_celebrant_greeting(db, occasion)
        merged = False

    label = args.get("custom_label") or occasion_type.value.replace("_", " ").title()
    total_friends = await db.scalar(
        select(func.count())
        .select_from(CelebrantFriend)
        .where(CelebrantFriend.celebrant_occasion_id == cel_occ.id)
    )
    saved_preferences = cel_occ.preferences_list
    saved_wishlists = cel_occ.wishlists
    result = {
        "success": True,
        "occasion_id": occasion.id,
        "celebrant_occasion_id": cel_occ.id,
        "occasion": label,
        "date": occasion_date.strftime("%d %B %Y"),
        "recurring": recurring,
        "preferences": saved_preferences,
        "friends_count": int(total_friends or 0),
        "reminders_created": sched["reminders_created"],
        "days_until": sched["days_until"],
        "skipped_past": sched.get("skipped_past", 0),
        "merged": merged,
        "message": (
            "Updated your existing occasion with the latest details — friends will be reminded "
            "without any duplicates!"
            if merged else
            (
                "Your occasion is saved. You can add gift preferences, wishlist links, "
                "or friends whenever you're ready."
                if not total_friends else
                "Your friends will be reminded before your occasion with your gift preferences!"
            )
        ),
    }
    if sched["tight_window"]:
        result["warning"] = "tight_window"
        result["hint"] = (
            f"Only {sched['days_until']} days left before {label}. Tell the user "
            f"that friends may have limited shipping windows (Amazon, Flipkart "
            f"need a few days). Offer to send an immediate reminder via "
            f"send_reminder_now so friends get the heads-up immediately."
        )
    if saved_wishlists:
        result["wishlists"] = saved_wishlists
    if cel_occ.note_to_friends:
        result["note"] = cel_occ.note_to_friends
    return result


async def update_celebrant_occasion(
    db: AsyncSession,
    phone: str,
    args: dict,
) -> dict:
    """Progressively edit an existing own occasion without re-supplying its date."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id:
        return {"error": "occasion_id is required"}
    occasion = await _user_owns_occasion(db, occasion_id, user.id)
    if not occasion or occasion.contact_id is not None:
        return _err(
            "occasion_not_found",
            _HINT_OCCASION_UNKNOWN,
            occasion_id=occasion_id,
        )
    cel_occ = (
        await db.execute(
            select(CelebrantOccasion).where(
                CelebrantOccasion.occasion_id == occasion.id
            )
        )
    ).scalar_one_or_none()
    if not cel_occ:
        return _err("occasion_not_found", _HINT_CELEBRANT_UNKNOWN)

    editable_fields = {
        "occasion_type",
        "date_day",
        "date_month",
        "date_year",
        "recurring",
        "custom_label",
        "preferences",
        "wishlist_urls",
        "note_to_friends",
        "add_friend_phones",
        "remove_friend_phones",
    }
    if not any(key in args for key in editable_fields):
        return {
            "error": "celebrant_updates_required",
            "message": "Pass at least one occasion, preference, wishlist, note, or friend update.",
        }

    new_recurring = (
        bool(args["recurring"])
        if "recurring" in args and args["recurring"] is not None
        else occasion.recurring
    )
    new_occasion_type = occasion.type
    if "occasion_type" in args:
        try:
            new_occasion_type = OccasionType(str(args["occasion_type"]))
        except ValueError:
            return {"error": f"Invalid occasion type: {args['occasion_type']}"}
    new_custom_label = occasion.custom_label
    if "custom_label" in args:
        new_custom_label = (
            str(args.get("custom_label") or "").strip() or None
        )
    if new_occasion_type == OccasionType.CUSTOM and not new_custom_label:
        return {"error": "custom_label is required when occasion_type is CUSTOM"}

    preferences = args.get("preferences")
    if "preferences" in args and not isinstance(preferences, list):
        return {"error": "preferences must be a list"}
    wishlist_urls = args.get("wishlist_urls")
    if "wishlist_urls" in args and not isinstance(wishlist_urls, list):
        return {"error": "wishlist_urls must be a list"}

    new_date = occasion.date
    date_changed = False
    if any(key in args for key in ("date_day", "date_month", "date_year")):
        try:
            day = int(args.get("date_day") or occasion.date.day)
            month = int(args.get("date_month") or occasion.date.month)
            year = int(args.get("date_year") or occasion.date.year)
            new_date = datetime(year, month, day, tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return {"error": "Invalid occasion date"}
        if occasion_calendar_date(new_date) < today_for_user(user):
            if new_recurring and args.get("date_year") is None:
                today = today_for_user(user)
                year = today.year + ((month, day) < (today.month, today.day))
                new_date = datetime(year, month, day, tzinfo=timezone.utc)
            else:
                return {
                    "error": "date_in_past",
                    "message": "A one-time occasion needs a current or future date.",
                }
        date_changed = new_date != occasion.date

    occasion.type = new_occasion_type
    occasion.custom_label = new_custom_label
    occasion.date = new_date
    if "recurring" in args and args["recurring"] is not None:
        date_changed = date_changed or occasion.recurring != new_recurring
        occasion.recurring = new_recurring

    if "preferences" in args:
        cel_occ.preferences = json.dumps(preferences)
    if "wishlist_urls" in args:
        cel_occ.wishlist_urls = (
            json.dumps(wishlist_urls) if wishlist_urls else None
        )
    if "note_to_friends" in args:
        cel_occ.note_to_friends = (
            str(args.get("note_to_friends") or "").strip() or None
        )

    add_phones, phone_to_name = _clean_friend_phones(
        args.get("add_friend_phones"),
        self_phone=phone,
    )
    remove_phones, _ = _clean_friend_phones(
        args.get("remove_friend_phones"),
        self_phone=phone,
    )
    remove_set = set(remove_phones)

    existing_friends = (
        await db.execute(
            select(CelebrantFriend).where(
                CelebrantFriend.celebrant_occasion_id == cel_occ.id
            )
        )
    ).scalars().all()
    existing_by_phone = {friend.phone: friend for friend in existing_friends}
    removed = [
        friend
        for friend in existing_friends
        if friend.phone in remove_set
    ]
    for friend in removed:
        await db.delete(friend)
    if remove_set:
        await db.execute(
            delete(Reminder).where(
                Reminder.occasion_id == occasion.id,
                Reminder.target_phone.in_(remove_set),
                Reminder.status == ReminderStatus.PENDING,
            )
        )

    new_phones = [
        value
        for value in add_phones
        if value not in existing_by_phone and value not in remove_set
    ]
    for friend_phone in new_phones:
        friend_user = (
            await db.execute(
                select(User).where(User.phone == friend_phone)
            )
        ).scalar_one_or_none()
        db.add(
            CelebrantFriend(
                id=generate_id("cfr"),
                celebrant_occasion_id=cel_occ.id,
                phone=friend_phone,
                user_id=friend_user.id if friend_user else None,
                name=(
                    friend_user.name
                    if friend_user
                    else phone_to_name.get(friend_phone)
                ),
            )
        )
    db.add_all([occasion, cel_occ])
    await db.flush()

    current_friend_phones = sorted(
        (
            set(existing_by_phone)
            - remove_set
        )
        | set(new_phones)
    )
    if date_changed:
        await db.execute(
            delete(Reminder).where(
                Reminder.occasion_id == occasion.id,
                Reminder.status == ReminderStatus.PENDING,
            )
        )
        sched = await schedule_reminders(
            db,
            occasion,
            target_phones=[None, *current_friend_phones],
        )
        await schedule_celebrant_greeting(db, occasion)
    elif new_phones:
        sched = await schedule_reminders(
            db,
            occasion,
            target_phones=new_phones,
        )
    else:
        sched = {
            "reminders_created": 0,
            "tight_window": False,
            "days_until": (
                occasion_calendar_date(occasion.date)
                - today_for_user(user)
            ).days,
            "skipped_past": 0,
        }

    return {
        "success": True,
        "occasion_id": occasion.id,
        "celebrant_occasion_id": cel_occ.id,
        "date": occasion.date.strftime("%d %B %Y"),
        "recurring": occasion.recurring,
        "preferences": cel_occ.preferences_list,
        "wishlists": cel_occ.wishlists,
        "note_to_friends": cel_occ.note_to_friends,
        "friends_count": len(current_friend_phones),
        "friends_added": len(new_phones),
        "friends_removed": len(removed),
        "reminders_created": sched["reminders_created"],
    }


async def get_celebrant_context(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get celebrant's occasion details for friends responding to reminders."""
    cel_occ_id = args.get("celebrant_occasion_id")
    if not cel_occ_id:
        return {"error": "celebrant_occasion_id is required"}

    cel_occ_res = await db.execute(
        select(CelebrantOccasion).where(CelebrantOccasion.id == cel_occ_id)
    )
    cel_occ = cel_occ_res.scalar_one_or_none()
    if not cel_occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    # Authorization: the caller must be an invited friend on this celebrant
    # occasion. Otherwise a prompt-injected LLM could pass a guessed
    # cel_occ_id and read another celebrant's preferences / wishlists.
    friend_row = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == phone,
        )
    )
    if friend_row.scalar_one_or_none() is None:
        return _err("not_invited", _HINT_OCCASION_UNKNOWN)

    occ_result = await db.execute(select(Occasion).where(Occasion.id == cel_occ.occasion_id))
    occasion = occ_result.scalar_one_or_none()
    if not occasion:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    user_result = await db.execute(select(User).where(User.id == occasion.user_id))
    celebrant = user_result.scalar_one_or_none()

    label = occasion.custom_label or occasion.type.value.replace("_", " ").title()
    prefs = cel_occ.preferences_list

    # Get already purchased gifts
    purchased = await get_purchased_categories(db, occasion.id)
    already_bought = []
    for gtype, brands in purchased.items():
        if brands:
            already_bought.extend(brands)

    # celebrant_phone is intentionally omitted — the friend never needs it
    # (the gift flows over Wishkr) and exposing it makes this function a
    # phone-lookup oracle if the friend-membership check is ever weakened.
    payload = {
        "celebrant_name": celebrant.name if celebrant else "Your friend",
        "occasion": label,
        "occasion_id": occasion.id,
        "date": occasion.date.strftime("%d %B %Y"),
        "preferences": prefs,
        "wishlists": cel_occ.wishlists,
        "already_purchased": already_bought,
        "gift_amount_note": "You can choose any gift amount — it's your choice!",
    }
    if cel_occ.note_to_friends:
        payload["note_from_celebrant"] = cel_occ.note_to_friends
    return payload
