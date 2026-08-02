import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Contact, Occasion, OccasionType, default_recurring_for_occasion, Order, OrderStatus,
    CelebrantOccasion, CelebrantFriend, Reminder, ReminderStatus,
    Purchase, ExternalGiftClaim, WishlistItem, OccasionGiftPlan,
    RecipientPreference, User,
)
from app.services.reminder import schedule_reminders
from app.utils.id_gen import generate_id
from app.utils.dates import next_occurrence
from app.utils.priority import priority_score
from app.utils.cities import resolve_city_id
from app.utils.timezones import (
    local_month_bounds_utc,
    occasion_calendar_date,
    timezone_for_user,
    today_for_user,
)
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_REGISTER, _HINT_OCCASION_UNKNOWN,
    _get_user, _find_or_create_contact, _user_owns_occasion,
)
from app.bot.state_utils import (
    ACTIVE_GIFT_CONTEXT_KEY,
    clear_occasion_slots,
    clear_state,
    get_gift_need_profile,
    get_state,
    remember_occasion_slots,
    update_active_gift_context,
)

_GENERIC_RELATION_NAMES = {
    "brother",
    "cousin",
    "dad",
    "father",
    "friend",
    "grandfather",
    "grandma",
    "grandmother",
    "grandpa",
    "husband",
    "mom",
    "mother",
    "sister",
    "wife",
}

_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}

_AMBIGUOUS_RELATION_NAMES = {
    "brother",
    "cousin",
    "friend",
    "grandfather",
    "grandma",
    "grandmother",
    "grandpa",
    "sister",
}

def _normalized_contact_name(name: str | None) -> str:
    return " ".join((name or "").strip().lower().split())


def _is_generic_relation_name(name: str | None) -> bool:
    return _normalized_contact_name(name) in _GENERIC_RELATION_NAMES


def _is_ambiguous_relation_name(name: str | None) -> bool:
    return _normalized_contact_name(name) in _AMBIGUOUS_RELATION_NAMES


def _reminder_merge_key(
    target_phone: str | None,
    message_type: str | None,
    scheduled_at: datetime | None,
    status: ReminderStatus | str | None,
) -> tuple[str, str, datetime | None, str]:
    scheduled_key = scheduled_at.replace(tzinfo=None) if scheduled_at else None
    status_key = status.value if isinstance(status, ReminderStatus) else (status or "")
    return (target_phone or "", message_type or "", scheduled_key, status_key)


async def _activate_saved_occasion(
    db: AsyncSession,
    phone: str,
    updates: dict,
) -> None:
    """Make a saved occasion the active recipient without touching gift plans."""
    # The occasion is saved — the half-finished slot fill is done, so drop the
    # `awaiting` marker. Leaving it armed would keep suppressing legitimate
    # "my occasions" requests on later turns.
    await clear_occasion_slots(db, phone)
    state = await get_state(db, phone)
    active = get_gift_need_profile(state)
    next_occasion_id = str(updates.get("occasion_id") or "").strip()
    if (
        active
        and next_occasion_id
        and active.occasion_id != next_occasion_id
    ):
        await clear_state(db, phone, ACTIVE_GIFT_CONTEXT_KEY)
        await clear_state(db, phone, "budget")
    await update_active_gift_context(
        db,
        phone,
        {
            **updates,
            "_explicit_context_change": True,
        },
        sync_gift_plan=False,
    )


async def add_occasion(db: AsyncSession, phone: str, args: dict) -> dict:
    """Save a new occasion for someone (birthday, anniversary, etc)."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    contact_name = args.get("contact_name", "").strip()
    occasion_type_str = args.get("occasion_type", "BIRTHDAY")
    date_day = args.get("date_day")
    date_month = args.get("date_month")

    if not contact_name:
        await remember_occasion_slots(db, phone, {
            "occasion_type": occasion_type_str,
            "relationship": args.get("relationship"),
        })
        return {"error": "Contact name is required"}
    if _is_ambiguous_relation_name(contact_name):
        # The agent is about to ask for the real name. Park what we already
        # know ("brother", BIRTHDAY) so the reply carrying the name isn't
        # hijacked by the router and the relation survives the round trip.
        await remember_occasion_slots(db, phone, {
            "relationship": args.get("relationship") or contact_name,
            "occasion_type": occasion_type_str,
        })
        return {
            "error": "specific_contact_name_required",
            "required_action": (
                f"Ask for the person's name before saving. For example: "
                f"\"What is your {contact_name}'s name?\""
            ),
            "message": (
                f"I need the person's name so your occasion list and reminders "
                f"are clear. What is your {contact_name}'s name?"
            ),
        }
    if not date_day or not date_month:
        await remember_occasion_slots(db, phone, {
            "contact_name": contact_name,
            "relationship": args.get("relationship"),
            "occasion_type": occasion_type_str,
        })
        return {"error": "Date (day and month) is required"}

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

    # "03/10/2000" is genuinely ambiguous (3 Oct or 10 Mar?) and a silently-wrong
    # birthday misfires every reminder for that person for years. Ambiguity is a
    # property of how the USER TYPED it, not of the parsed numbers — "3rd
    # October" is unambiguous even though both parts are <= 12 — so the agent
    # flags the numeric slash form via date_input_numeric and we confirm once.
    if (
        args.get("date_input_numeric")
        and date_day <= 12
        and date_month <= 12
        and date_day != date_month
        and not args.get("date_confirmed")
    ):
        return {
            "error": "date_ambiguous",
            "required_action": (
                "Confirm the reading with the user, then call add_occasion again "
                "with date_confirmed=true (swapping date_day/date_month if they "
                "meant the other reading)."
            ),
            "message": (
                f"Just to confirm — {occasion_date.strftime('%-d %B')}? "
                f"(If you meant {date_month} {_MONTH_NAMES[date_day]}, tell me.)"
            ),
        }

    # User-local date comparison: at 4am IST on Apr 29 (UTC 22:30 Apr 28) the user
    # who types "Apr 28" means next year's Apr 28, not today's already-past
    # one. UTC ``now.date()`` would still be Apr 28 and we'd keep this year.
    if (
        occasion_calendar_date(occasion_date) < today
        and (recurring or date_year is None)
    ):
        # Roll to the next FUTURE occurrence, not simply year+1: a birth year
        # (2000) would otherwise land on 2001 and schedule reminders decades in
        # the past.
        # ponytail: the birth year itself is returned to the agent but not
        # persisted — Contact has no birth_year column. Add one (+ migration)
        # when age-aware gifting needs it beyond the current turn.
        next_year = today.year if (date_month, date_day) >= (today.month, today.day) else today.year + 1
        try:
            occasion_date = datetime(next_year, date_month, date_day, tzinfo=timezone.utc)
        except ValueError:
            # 29 Feb in a non-leap target year — fall back to 28 Feb.
            occasion_date = datetime(next_year, date_month, 28, tzinfo=timezone.utc)
    elif occasion_calendar_date(occasion_date) < today:
        return {
            "error": "date_in_past",
            "message": "A one-time occasion needs a current or future date.",
        }

    # Alias-aware dedupe: if the user first saved "mom's birthday" and
    # later says "her name is Asha, birthday is today", don't create a
    # parallel Asha row for the same birthday. Treat the specific name as
    # a contact-name correction on the existing occasion. (Ambiguous
    # relations like sister/cousin are rejected earlier, so only single-
    # person relations such as mom/dad/wife reach this merge.)
    same_day_q = (
        select(Occasion, Contact)
        .join(Contact, Occasion.contact_id == Contact.id)
        .where(
            Occasion.user_id == user.id,
            Occasion.type == occasion_type,
            func.extract("month", Occasion.date) == occasion_date.month,
            func.extract("day", Occasion.date) == occasion_date.day,
            # A soft-deleted occasion must not absorb a fresh add as a
            # "correction" — that would silently revive/expose a row the owner
            # deleted. Let the new add create its own occasion instead.
            Occasion.deleted_at.is_(None),
        )
        .order_by(Occasion.created_at.desc())
    )
    incoming_name_norm = _normalized_contact_name(contact_name)
    for existing_occ, existing_contact in (await db.execute(same_day_q)).all():
        existing_name_norm = _normalized_contact_name(existing_contact.name)
        same_name = existing_name_norm == incoming_name_norm
        generic_to_specific = (
            _is_generic_relation_name(existing_contact.name)
            and incoming_name_norm
            and not _is_generic_relation_name(contact_name)
        )
        specific_to_generic = (
            _is_generic_relation_name(contact_name)
            and existing_name_norm
            and not _is_generic_relation_name(existing_contact.name)
        )
        if not (same_name or generic_to_specific or specific_to_generic):
            continue

        renamed = False
        if generic_to_specific:
            existing_contact.name = contact_name
            db.add(existing_contact)
            renamed = True
        recipient_city = (args.get("recipient_city") or "").strip()
        if recipient_city:
            resolution = await resolve_city_id(db, recipient_city)
            existing_contact.city_id = resolution.city_id
            existing_contact.city_raw = resolution.city_raw
            db.add(existing_contact)
        if recurring_arg is not None:
            existing_occ.recurring = recurring
        db.add(existing_occ)
        await db.flush()

        label = args.get("custom_label") or occasion_type.value.replace("_", " ").title()
        await _activate_saved_occasion(
            db,
            phone,
            {
                "recipient_name": existing_contact.name,
                "recipient_phone": existing_contact.phone,
                "recipient_city": existing_contact.city_id or existing_contact.city_raw,
                "occasion_id": existing_occ.id,
                "occasion_type": label,
                "occasion_date": existing_occ.date.strftime("%Y-%m-%d"),
                "purpose": "gift_for_friend",
                "current_step": "occasion_saved",
            },
        )
        return {
            "success": True,
            "reused": True,
            "merged": generic_to_specific,
            "occasion_id": existing_occ.id,
            "contact_id": existing_contact.id,
            "contact_name": existing_contact.name,
            "occasion": label,
            "date": existing_occ.date.strftime("%d %B %Y"),
            "recurring": existing_occ.recurring,
            "recipient_city": existing_contact.city_raw,
            "reminders_scheduled": True,
            "message": (
                f"Updated the existing {label} for {existing_contact.name}."
                if renamed else
                f"Already had this {label} for {existing_contact.name}."
            ),
        }

    # Create or find contact
    contact_phone = args.get("contact_phone")
    if contact_phone:
        phone_clean = "".join(c for c in contact_phone if c.isdigit())
        if len(phone_clean) == 10:
            contact_phone = "91" + phone_clean
        elif len(phone_clean) == 12 and phone_clean.startswith("91"):
            contact_phone = phone_clean
        else:
            contact_phone = None

    contact = await _find_or_create_contact(
        db, user.id, contact_name, contact_phone, args.get("relationship"),
    )
    recipient_city = (args.get("recipient_city") or "").strip()
    if recipient_city:
        resolution = await resolve_city_id(db, recipient_city)
        contact.city_id = resolution.city_id
        contact.city_raw = resolution.city_raw
        db.add(contact)
        await db.flush()

    # Dedupe guard: if the same user just added the same {contact, type} in the
    # last 5 minutes, treat as a correction. Returning the existing id lets the
    # agent call edit_occasion instead of creating a duplicate row. See
    # documentation/issues/005-duplicate-occasion-on-date-correction.md
    recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    dup_q = (
        select(Occasion)
        .where(
            Occasion.user_id == user.id,
            Occasion.contact_id == contact.id,
            Occasion.type == occasion_type,
            Occasion.created_at >= recent_cutoff,
            Occasion.deleted_at.is_(None),
        )
        .order_by(Occasion.created_at.desc())
    )
    recent_dup = (await db.execute(dup_q)).scalars().first()
    if recent_dup:
        existing_date = recent_dup.date.strftime("%d %B %Y") if recent_dup.date else "unknown"
        new_date = occasion_date.strftime("%d %B %Y")
        return {
            "warning": "duplicate_recent",
            "existing_occasion_id": recent_dup.id,
            "existing_date": existing_date,
            "intended_new_date": new_date,
            "message": (
                f"You added a {occasion_type.value.lower()} for {contact_name} on "
                f"{existing_date} just now. If you meant to correct the date to "
                f"{new_date}, call edit_occasion on {recent_dup.id} instead of add_occasion."
            ),
        }

    occasion = Occasion(
        id=generate_id("occ"),
        user_id=user.id,
        contact_id=contact.id,
        type=occasion_type,
        custom_label=args.get("custom_label"),
        date=occasion_date,
        recurring=recurring,
    )
    db.add(occasion)
    await db.flush()

    sched = await schedule_reminders(db, occasion)

    label = args.get("custom_label") or occasion_type.value.replace("_", " ").title()
    await _activate_saved_occasion(
        db,
        phone,
        {
            "recipient_name": contact_name,
            "recipient_phone": contact.phone,
            "recipient_city": contact.city_id or contact.city_raw,
            "occasion_id": occasion.id,
            "occasion_type": label,
            "occasion_date": occasion_date.strftime("%Y-%m-%d"),
            "purpose": "gift_for_friend",
            "current_step": "occasion_saved",
        },
    )
    result_out = {
        "success": True,
        "occasion_id": occasion.id,
        "contact_name": contact_name,
        "occasion": label,
        "date": occasion_date.strftime("%d %B %Y"),
        # Age is the strongest signal for tailoring gift ideas; surface it for
        # this turn when the user gave a birth year (not persisted — see above).
        **(
            {"birth_year": year, "age_turning": occasion_date.year - year}
            if date_year is not None and occasion_type == OccasionType.BIRTHDAY
            else {}
        ),
        "recurring": recurring,
        "recipient_city": contact.city_raw,
        "reminders_scheduled": True,
        "reminders_created": sched["reminders_created"],
        "days_until": sched["days_until"],
        "skipped_past": sched.get("skipped_past", 0),
    }
    if sched["tight_window"]:
        result_out["warning"] = "tight_window"
        result_out["hint"] = (
            f"Only {sched['days_until']} days are left before {label}, and "
            f"{sched['skipped_past']} default reminder time(s) have already "
            f"passed. Do not say an immediate reminder was scheduled. State "
            f"only what is confirmed; call get_reminder_schedule if exact "
            f"dates are needed. If the user requests different offsets, use "
            f"set_reminder_schedule."
        )
    return result_out


async def list_occasions(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get all upcoming occasions for the current user."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Occasion)
        .where(Occasion.user_id == user.id, Occasion.deleted_at.is_(None))
        .order_by(Occasion.date)
    )
    all_occasions = result.scalars().all()

    today = today_for_user(user, now)
    with_next = [
        (o, next_occurrence(o, now, reference_date=today))
        for o in all_occasions
    ]
    upcoming = sorted(
        [
            (o, nxt)
            for o, nxt in with_next
            if occasion_calendar_date(nxt) >= today
        ],
        key=lambda pair: pair[1],
    )[:15]

    if not upcoming:
        return {"occasions": [], "message": "No upcoming occasions"}

    items = []
    for occ, nxt in upcoming:
        contact_name = "You"
        if occ.contact_id:
            c_result = await db.execute(select(Contact).where(Contact.id == occ.contact_id))
            contact = c_result.scalar_one_or_none()
            if contact:
                contact_name = contact.name

        label = occ.custom_label or occ.type.value.replace("_", " ").title()
        items.append({
            "id": occ.id,
            "contact_name": contact_name,
            "occasion": label,
            "date": nxt.strftime("%d %B %Y"),
            "recurring": occ.recurring,
        })

    return {"occasions": items}


async def get_my_occasions(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get all occasions saved by the user with basic info."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    # Get all occasions
    occ_result = await db.execute(
        select(Occasion)
        .where(Occasion.user_id == user.id, Occasion.deleted_at.is_(None))
        .order_by(Occasion.date)
    )
    occasions = occ_result.scalars().all()

    if not occasions:
        return {"occasions": [], "message": "No occasions saved yet"}

    now = datetime.now(timezone.utc)
    today = today_for_user(user, now)
    with_next = sorted(
        [
            (o, next_occurrence(o, now, reference_date=today))
            for o in occasions
        ],
        key=lambda pair: pair[1],
    )

    items = []
    for occ, nxt in with_next:
        # Determine whose occasion
        if occ.contact_id:
            c_result = await db.execute(select(Contact).where(Contact.id == occ.contact_id))
            contact = c_result.scalar_one_or_none()
            whose = contact.name if contact else "Someone"
            relation = contact.relationship_type if contact else None
            occasion_type = "for_other"
        else:
            whose = user.name or phone
            relation = None
            occasion_type = "own"  # User's own occasion (celebrant)

        label = occ.custom_label or occ.type.value.replace("_", " ").title()
        items.append({
            "occasion_id": occ.id,
            "whose": whose,
            "relation": relation,
            "type": occasion_type,
            "occasion": label,
            "date": nxt.strftime("%d %B %Y") if occ.date else "Unknown",
        })

    return {"occasions": items, "total": len(items)}


async def get_occasion_details(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get detailed info for a specific occasion including friends to remind."""
    occasion_id = args.get("occasion_id")
    if not occasion_id:
        return {"error": "occasion_id is required"}

    # Authorization: only the occasion owner may read full details (including
    # contact phone, friends-to-remind phones, wishlists). Without this guard,
    # any caller — or an LLM coaxed by prompt injection from a wishlist URL
    # title or contact name — could enumerate occasion ids and leak PII.
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_found", _HINT_OCCASION_UNKNOWN)

    occasion = await _user_owns_occasion(db, occasion_id, user.id)
    if not occasion:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    # Basic info
    label = occasion.custom_label or occasion.type.value.replace("_", " ").title()
    info = {
        "occasion_id": occasion.id,
        "occasion": label,
        "date": occasion.date.strftime("%d %B %Y") if occasion.date else "Unknown",
        "recurring": occasion.recurring,
    }
    recipient_wishlist_urls: set[str] = set()

    # Whose occasion?
    if occasion.contact_id:
        c_result = await db.execute(select(Contact).where(Contact.id == occasion.contact_id))
        contact = c_result.scalar_one_or_none()
        info["whose"] = contact.name if contact else "Someone"
        info["type"] = "for_other"
        info["contact_id"] = occasion.contact_id
        if contact and contact.phone:
            info["contact_phone"] = contact.phone
        if contact and contact.relationship_type:
            info["relationship"] = contact.relationship_type
        if contact and (contact.city_id or contact.city_raw):
            info["contact_city"] = contact.city_id or contact.city_raw
        info["recipient_preferences_available"] = False
        if contact and contact.phone:
            preference = (
                await db.execute(
                    select(RecipientPreference).where(
                        RecipientPreference.recipient_phone == contact.phone
                    )
                )
            ).scalar_one_or_none()
            info["recipient_preferences_available"] = preference is not None
            if preference and preference.wishlist_links:
                try:
                    links = json.loads(preference.wishlist_links)
                except (json.JSONDecodeError, TypeError):
                    links = []
                if isinstance(links, list):
                    for link in links:
                        raw_url = (
                            link.get("url")
                            if isinstance(link, dict)
                            else link
                        )
                        if isinstance(raw_url, str) and raw_url.strip():
                            recipient_wishlist_urls.add(raw_url.strip())
            recipient_user = (
                await db.execute(
                    select(User).where(User.phone == contact.phone)
                )
            ).scalar_one_or_none()
            if recipient_user:
                own_urls = (
                    await db.execute(
                        select(WishlistItem.url).where(
                            WishlistItem.user_id == recipient_user.id,
                            WishlistItem.purpose == "self",
                            # A giver's own contact occasion must not reveal
                            # that the recipient has private wishlist rows.
                            WishlistItem.visibility == "public",
                            WishlistItem.gifted_at.is_(None),
                            WishlistItem.unavailable_at.is_(None),
                        )
                    )
                ).scalars().all()
                recipient_wishlist_urls.update(
                    str(url).strip() for url in own_urls if str(url).strip()
                )
    else:
        # User's own occasion - get celebrant details
        u_result = await db.execute(select(User).where(User.id == occasion.user_id))
        owner = u_result.scalar_one_or_none()
        info["whose"] = owner.name if owner else "You"
        info["type"] = "own"

        # Get celebrant occasion details (preferences, friends)
        cel_result = await db.execute(
            select(CelebrantOccasion).where(CelebrantOccasion.occasion_id == occasion.id)
        )
        cel_occ = cel_result.scalar_one_or_none()

        if cel_occ:
            info["preferences"] = cel_occ.preferences_list
            info["wishlists"] = cel_occ.wishlists
            recipient_wishlist_urls.update(
                str(item.get("url", "")).strip()
                for item in cel_occ.wishlists
                if isinstance(item, dict) and str(item.get("url", "")).strip()
            )
            if cel_occ.note_to_friends:
                info["note_to_friends"] = cel_occ.note_to_friends

            # Get friends to remind
            fr_result = await db.execute(
                select(CelebrantFriend).where(CelebrantFriend.celebrant_occasion_id == cel_occ.id)
            )
            friends = fr_result.scalars().all()

            if friends:
                info["friends_to_remind"] = [
                    {
                        "name": f.name or "Unknown",
                        "phone": f.phone,
                        "status": f.status.value,
                        "reminded_at": f.reminded_at.strftime("%d %B %Y") if f.reminded_at else None,
                    }
                    for f in friends
                ]
                info["friends_count"] = len(friends)

        own_urls = (
            await db.execute(
                select(WishlistItem.url).where(
                    WishlistItem.user_id == user.id,
                    WishlistItem.purpose == "self",
                    WishlistItem.own_occasion_id == occasion.id,
                    WishlistItem.gifted_at.is_(None),
                    WishlistItem.unavailable_at.is_(None),
                )
            )
        ).scalars().all()
        recipient_wishlist_urls.update(
            str(url).strip() for url in own_urls if str(url).strip()
        )

    info["recipient_wishlist_count"] = len(recipient_wishlist_urls)

    reminder_count_rows = (
        await db.execute(
            select(Reminder.status, func.count(Reminder.id))
            .where(Reminder.occasion_id == occasion.id)
            .group_by(Reminder.status)
        )
    ).all()
    reminder_counts = {
        status: int(count)
        for status, count in reminder_count_rows
    }
    info["reminder_schedule"] = {
        "total_count": sum(reminder_counts.values()),
        "pending_count": reminder_counts.get(ReminderStatus.PENDING, 0),
        "sent_count": reminder_counts.get(ReminderStatus.SENT, 0),
    }

    gift_plan = (
        await db.execute(
            select(OccasionGiftPlan).where(
                OccasionGiftPlan.user_id == user.id,
                OccasionGiftPlan.occasion_id == occasion.id,
            )
        )
    ).scalar_one_or_none()
    if gift_plan:
        info["gift_plan"] = {
            "budget_min": gift_plan.budget_min,
            "budget_max": gift_plan.budget_max,
            "currency": gift_plan.currency,
            "interests": gift_plan.interests or [],
            "vibes": gift_plan.vibe_ids or [],
            "desired_categories": gift_plan.desired_categories or [],
            "excluded_categories": gift_plan.excluded_categories or [],
            "preferred_brands": gift_plan.preferred_brands or [],
            "disliked_brands": gift_plan.disliked_brands or [],
            "gift_format": gift_plan.gift_format,
            "constraints": gift_plan.constraints or {},
            "recipient_city": gift_plan.recipient_city,
            "selected_brand": gift_plan.selected_brand,
            "current_step": gift_plan.current_step,
            "last_suggestion_names": [
                item.get("name")
                for item in (gift_plan.last_suggestions or [])
                if isinstance(item, dict) and item.get("name")
            ],
        }

    giver_shortlist_count = await db.scalar(
        select(func.count())
        .select_from(WishlistItem)
        .where(
            WishlistItem.user_id == user.id,
            WishlistItem.purpose == "gift",
            WishlistItem.recipient_occasion_id == occasion.id,
            WishlistItem.gifted_at.is_(None),
        )
    )
    info["giver_shortlist_count"] = int(giver_shortlist_count or 0)

    return info


async def get_upcoming_digest(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get user's upcoming occasions (next N days) sorted by priority.

    Priority = relationship_weight x urgency_weight.
    Filters out snoozed occasions and those already skipped this year.
    Returns OTHERS' occasions by default. Pass include_own=True to also include
    the user's own occasions (e.g. when they ask 'when's my birthday reminder?').
    """
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    days = int(args.get("days", 7))
    include_own = bool(args.get("include_own", False))
    now = datetime.now(timezone.utc)
    today = today_for_user(user, now)
    window_end = today + timedelta(days=days)
    current_year = str(today.year)

    # Pull all user occasions — we filter by next_occurrence() in Python so
    # recurring birthdays with past-dated stored rows still show up.
    query = (
        select(Occasion, Contact)
        .outerjoin(Contact, Occasion.contact_id == Contact.id)
        .where(Occasion.user_id == user.id, Occasion.deleted_at.is_(None))
    )
    if not include_own:
        query = query.where(Occasion.contact_id.is_not(None))

    result = await db.execute(query)
    rows = result.all()

    items = []
    # Nearest occasion beyond the window — surfaced when the window is
    # empty so the agent never dead-ends on "nothing in 7 days" while an
    # occasion sits on day 8 (2026-07-23 incident: wedding 8 days out).
    next_beyond: dict | None = None
    for occ, contact in rows:
        # Skip snoozed
        if occ.snooze_until and occ.snooze_until > now:
            continue
        # Skip if already skipped this year
        if occ.skipped_year == current_year:
            continue

        effective_date = next_occurrence(
            occ,
            now,
            reference_date=today,
        )
        effective_calendar_date = occasion_calendar_date(effective_date)
        if effective_calendar_date < today:
            continue
        if effective_calendar_date > window_end:
            if next_beyond is None or effective_date < next_beyond["_dt"]:
                next_beyond = {
                    "_dt": effective_date,
                    "occasion_id": occ.id,
                    "contact_name": contact.name if contact else "Yours",
                    "type": occ.type.value,
                    "custom_label": occ.custom_label,
                    "date": effective_date.strftime("%a %d %b %Y"),
                    "days_until": max(
                        0,
                        (
                            occasion_calendar_date(effective_date)
                            - today
                        ).days,
                    ),
                }
            continue

        rel = contact.relationship_type if contact else None
        score = priority_score(
            effective_date,
            rel,
            reference_date=today,
        )
        # User-local day diff avoids off-by-one copy around UTC midnight.
        # window where UTC-naive math is off by one against the user's
        # local calendar.
        days_until = max(
            0,
            (
                occasion_calendar_date(effective_date)
                - today
            ).days,
        )

        items.append({
            "occasion_id": occ.id,
            "contact_name": contact.name if contact else "Yours",
            "is_own": contact is None,
            "relationship": rel or ("self" if contact is None else "friend"),
            "type": occ.type.value,
            "custom_label": occ.custom_label,
            "date": effective_date.strftime("%a %d %b %Y"),
            "days_until": days_until,
            "priority_score": round(score, 1),
        })

    items.sort(key=lambda x: -x["priority_score"])

    payload = {
        "total_upcoming": len(items),
        "window_days": days,
        "occasions": items,
    }
    if not items and next_beyond is not None:
        next_beyond.pop("_dt")
        payload["next_occasion_beyond_window"] = next_beyond
        payload["hint"] = (
            "The window is empty but a later occasion exists — mention it "
            "instead of saying there are no upcoming occasions."
        )
    return payload


async def snooze_occasion(db: AsyncSession, phone: str, args: dict) -> dict:
    """Snooze an occasion reminder until a specific date."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = args.get("occasion_id")
    days = int(args.get("days", 2))

    if not occasion_id:
        return {"error": "occasion_id is required"}

    result = await db.execute(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    occ = result.scalar_one_or_none()
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    occ.snooze_until = datetime.now(timezone.utc) + timedelta(days=days)
    db.add(occ)
    await db.flush()

    return {
        "success": True,
        "occasion_id": occasion_id,
        "snoozed_until": occ.snooze_until.strftime("%d %b %Y"),
        "message": f"Snoozed for {days} day{'s' if days != 1 else ''}.",
    }


async def skip_occasion_this_year(db: AsyncSession, phone: str, args: dict) -> dict:
    """Mark an occasion as skipped for the current year (won't remind again until next year)."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = args.get("occasion_id")
    if not occasion_id:
        return {"error": "occasion_id is required"}

    result = await db.execute(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    occ = result.scalar_one_or_none()
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    occ.skipped_year = str(today_for_user(user).year)
    db.add(occ)
    await db.flush()

    return {
        "success": True,
        "occasion_id": occasion_id,
        "message": "Skipped for this year. I'll remind you again next year!",
    }


async def get_monthly_spend(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get user's total gift spend for the current month (paid orders only)."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    now = datetime.now(timezone.utc)
    local_now = now.astimezone(timezone_for_user(user))
    month_start, next_month_start = local_month_bounds_utc(user, now)

    result = await db.execute(
        select(Order).where(
            Order.user_id == user.id,
            Order.created_at >= month_start,
            Order.created_at < next_month_start,
            Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED]),
        )
    )
    orders = result.scalars().all()

    total = sum(float(o.total_amount or 0) for o in orders)
    return {
        "month": local_now.strftime("%B %Y"),
        "total_spend": total,
        "order_count": len(orders),
    }


async def edit_occasion(db: AsyncSession, phone: str, args: dict) -> dict:
    """Update fields on an existing occasion (date, type, custom_label, contact_name)."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = args.get("occasion_id")
    if not occasion_id:
        return {"error": "occasion_id is required"}

    result = await db.execute(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    occ = result.scalar_one_or_none()
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    changes = []

    date_day = args.get("date_day")
    date_month = args.get("date_month")
    if date_day and date_month:
        try:
            today = today_for_user(user)
            new_date = datetime(
                today.year,
                int(date_month),
                int(date_day),
                tzinfo=timezone.utc,
            )
            if occasion_calendar_date(new_date) < today:
                new_date = datetime(
                    today.year + 1,
                    int(date_month),
                    int(date_day),
                    tzinfo=timezone.utc,
                )
            occ.date = new_date
            changes.append(f"date → {new_date.strftime('%d %b')}")
        except ValueError:
            return {"error": f"Invalid date: {date_day}/{date_month}"}

    occasion_type_str = args.get("occasion_type")
    if occasion_type_str:
        try:
            occ.type = OccasionType(occasion_type_str)
            changes.append(f"type → {occasion_type_str}")
        except ValueError:
            return {"error": f"Invalid occasion type: {occasion_type_str}"}

    custom_label = args.get("custom_label")
    if custom_label is not None:
        occ.custom_label = custom_label.strip() or None
        changes.append(f"label → {custom_label}")

    contact_name = args.get("contact_name")
    if contact_name and occ.contact_id:
        contact_result = await db.execute(
            select(Contact).where(Contact.id == occ.contact_id)
        )
        contact = contact_result.scalar_one_or_none()
        if contact:
            contact.name = contact_name.strip()
            db.add(contact)
            changes.append(f"contact name → {contact_name}")

    if not changes:
        return {"error": "No changes provided — specify at least one field to update"}

    db.add(occ)
    await db.flush()

    return {
        "success": True,
        "occasion_id": occasion_id,
        "changes": changes,
        "message": f"Updated: {', '.join(changes)}",
    }


async def delete_occasion(db: AsyncSession, phone: str, args: dict) -> dict:
    """Delete a saved occasion (and its pending reminders)."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    occasion_id = args.get("occasion_id")
    if not occasion_id:
        return {"error": "occasion_id is required"}

    result = await db.execute(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    occ = result.scalar_one_or_none()
    if not occ:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    label = occ.custom_label or occ.type.value.replace("_", " ").title()

    # If friends were invited to this occasion, hard-deleting it would cascade
    # away the celebrant_occasion + celebrant_friends and leave every reminder
    # link those friends already received pointing at a dead ID (the friend
    # then loops on "re-open the reminder link"). Soft-delete instead: hide the
    # occasion from the owner but keep the row alive so get_celebrant_context
    # still resolves. Pending reminders are still removed so nobody gets nudged.
    has_friends = await db.scalar(
        select(func.count())
        .select_from(CelebrantFriend)
        .join(
            CelebrantOccasion,
            CelebrantOccasion.id == CelebrantFriend.celebrant_occasion_id,
        )
        .where(CelebrantOccasion.occasion_id == occasion_id)
    )

    await db.execute(sa_delete(Reminder).where(Reminder.occasion_id == occasion_id))
    if has_friends:
        occ.deleted_at = datetime.now(timezone.utc)
        db.add(occ)
    else:
        await db.delete(occ)
    await db.flush()

    return {
        "success": True,
        "message": f"Deleted '{label}' and its reminders.",
    }


async def merge_occasions(db: AsyncSession, phone: str, args: dict) -> dict:
    """Merge duplicate occasions into one canonical record."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    keep_id = args.get("keep_occasion_id")
    merge_id = args.get("merge_occasion_id")
    contact_name = (args.get("contact_name") or "").strip()

    if not keep_id or not merge_id:
        return {"error": "keep_occasion_id and merge_occasion_id are required"}
    if keep_id == merge_id:
        return {"error": "Cannot merge an occasion into itself"}

    keep_result = await db.execute(
        select(Occasion).where(
            Occasion.id == keep_id, Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    keep = keep_result.scalar_one_or_none()
    merge_result = await db.execute(
        select(Occasion).where(
            Occasion.id == merge_id, Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    duplicate = merge_result.scalar_one_or_none()
    if not keep or not duplicate:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    keep_day_month = (keep.date.day, keep.date.month)
    merge_day_month = (duplicate.date.day, duplicate.date.month)
    if keep.type != duplicate.type or keep_day_month != merge_day_month:
        return _err(
            "not_mergeable",
            "Only merge duplicate occasions with the same type and day/month. "
            "If the date or type is wrong, use edit_occasion instead.",
            keep_occasion_id=keep_id,
            merge_occasion_id=merge_id,
        )

    if contact_name and keep.contact_id:
        contact = await db.get(Contact, keep.contact_id)
        if contact:
            contact.name = contact_name
            db.add(contact)
    elif not keep.contact_id and duplicate.contact_id:
        keep.contact_id = duplicate.contact_id
    elif keep.contact_id and duplicate.contact_id:
        dup_contact = await db.get(Contact, duplicate.contact_id)
        keep_contact = await db.get(Contact, keep.contact_id)
        if keep_contact and dup_contact and dup_contact.phone and not keep_contact.phone:
            keep_contact.phone = dup_contact.phone
            db.add(keep_contact)

    if not keep.custom_label and duplicate.custom_label:
        keep.custom_label = duplicate.custom_label
    keep.snooze_until = keep.snooze_until or duplicate.snooze_until
    keep.skipped_year = keep.skipped_year or duplicate.skipped_year
    db.add(keep)

    reminder_result = await db.execute(
        select(Reminder).where(Reminder.occasion_id == duplicate.id)
    )
    moved_reminders = 0
    cancelled_duplicate_reminders = 0
    existing_reminder_keys = {
        _reminder_merge_key(row[0], row[1], row[2], row[3])
        for row in (
            await db.execute(
                select(
                    Reminder.target_phone,
                    Reminder.message_type,
                    Reminder.scheduled_at,
                    Reminder.status,
                ).where(Reminder.occasion_id == keep.id)
            )
        ).all()
    }
    for reminder in reminder_result.scalars().all():
        key = _reminder_merge_key(
            reminder.target_phone,
            reminder.message_type,
            reminder.scheduled_at,
            reminder.status,
        )
        if key in existing_reminder_keys:
            reminder.status = ReminderStatus.CANCELLED
            cancelled_duplicate_reminders += 1
        else:
            reminder.occasion_id = keep.id
            existing_reminder_keys.add(key)
            moved_reminders += 1
        db.add(reminder)

    purchase_result = await db.execute(
        select(Purchase).where(Purchase.occasion_id == duplicate.id)
    )
    moved_purchases = 0
    for purchase in purchase_result.scalars().all():
        purchase.occasion_id = keep.id
        db.add(purchase)
        moved_purchases += 1

    claim_result = await db.execute(
        select(ExternalGiftClaim).where(ExternalGiftClaim.occasion_id == duplicate.id)
    )
    moved_claims = 0
    for claim in claim_result.scalars().all():
        claim.occasion_id = keep.id
        db.add(claim)
        moved_claims += 1

    keep_wishlist_result = await db.execute(
        select(WishlistItem).where(WishlistItem.recipient_occasion_id == keep.id)
    )
    keep_wishlist_by_url: dict[tuple[str, str], WishlistItem] = {}
    keep_wishlist_by_product: dict[tuple[str, str], WishlistItem] = {}
    for item in keep_wishlist_result.scalars().all():
        keep_wishlist_by_url[(item.url, item.purpose)] = item
        if item.product_id:
            keep_wishlist_by_product[(item.product_id, item.purpose)] = item

    wishlist_result = await db.execute(
        select(WishlistItem).where(WishlistItem.recipient_occasion_id == duplicate.id)
    )
    moved_wishlist_items = 0
    deduped_wishlist_items = 0
    for wishlist_item in wishlist_result.scalars().all():
        existing_item = keep_wishlist_by_url.get((wishlist_item.url, wishlist_item.purpose))
        if not existing_item and wishlist_item.product_id:
            existing_item = keep_wishlist_by_product.get(
                (wishlist_item.product_id, wishlist_item.purpose)
            )
        if existing_item:
            await db.execute(
                update(Purchase)
                .where(Purchase.wishlist_item_id == wishlist_item.id)
                .values(wishlist_item_id=existing_item.id)
            )
            await db.execute(
                update(ExternalGiftClaim)
                .where(ExternalGiftClaim.wishlist_item_id == wishlist_item.id)
                .values(wishlist_item_id=existing_item.id)
            )
            if wishlist_item.gifted_at and not existing_item.gifted_at:
                existing_item.gifted_at = wishlist_item.gifted_at
                existing_item.gifted_order_id = wishlist_item.gifted_order_id
                db.add(existing_item)
            await db.delete(wishlist_item)
            deduped_wishlist_items += 1
            continue
        wishlist_item.recipient_occasion_id = keep.id
        db.add(wishlist_item)
        keep_wishlist_by_url[(wishlist_item.url, wishlist_item.purpose)] = wishlist_item
        if wishlist_item.product_id:
            keep_wishlist_by_product[(wishlist_item.product_id, wishlist_item.purpose)] = wishlist_item
        moved_wishlist_items += 1

    await db.execute(sa_delete(Reminder).where(Reminder.occasion_id == duplicate.id))
    await db.delete(duplicate)
    await db.flush()

    label = contact_name or keep.custom_label or keep.type.value.replace("_", " ").title()
    return {
        "success": True,
        "merged": True,
        "occasion_id": keep.id,
        "merged_occasion_id": duplicate.id,
        "moved_reminders": moved_reminders,
        "cancelled_duplicate_reminders": cancelled_duplicate_reminders,
        "moved_purchases": moved_purchases,
        "moved_claims": moved_claims,
        "moved_wishlist_items": moved_wishlist_items,
        "deduped_wishlist_items": deduped_wishlist_items,
        "message": f"Combined the duplicate entries into one occasion for {label}.",
    }
