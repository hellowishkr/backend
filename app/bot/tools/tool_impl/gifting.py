import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ExternalGiftClaim, GiftLog, Occasion
from app.bot.sources import SOURCES, GiftCandidate
from app.services.gift_recommendation import (
    FULFILLMENT_BONUS,
    GiftRecommendationRequest,
    budget_overlaps,
    recommend_gifts,
    score_candidate,
)
from app.services.hubble import HubbleUnavailableError, hubble_service
from app.utils.id_gen import generate_id
from app.utils.dedup import get_full_gift_checklist
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_REGISTER, _HINT_OCCASION_UNKNOWN,
    _get_user, _can_view_occasion_gifts,
)
from app.bot.state_utils import (
    ACTIVE_GIFT_CONTEXT_KEY,
    clear_state,
    get_gift_need_profile,
    get_state,
    has_active_gift_context,
    set_state,
    update_active_gift_context,
)

logger = logging.getLogger("wishkr.tools.functions")

# Fulfillment preference order — instant beats delivery beats booking
_FULFILLMENT_BONUS = FULFILLMENT_BONUS
_SUGGESTION_HISTORY_KEY = "gift_suggestion_history"
_CLARIFICATION_STATE_KEY = "gift_clarification_state"


def _user_visible_suggestions(result: dict) -> list[dict]:
    """Collect every suggestion the agent may show, without duplicate names."""
    groups: list[object] = [result.get("suggestions")]
    review = result.get("recommendation_review")
    if isinstance(review, dict):
        groups.extend([
            review.get("catalogue_options"),
            review.get("additional_general_ideas"),
        ])

    suggestions: list[dict] = []
    seen_names: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            normalized_name = name.lower()
            if not name or normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            suggestions.append(item)
    return suggestions


def _suggestion_history_signature(
    *,
    scenario: str,
    occasion_id: str | None,
    vibes: list[str],
    recipient_vibe: str,
    budget_min,
    budget_max,
    budget_text: str,
    profile,
) -> str:
    """Stable key for one gift-idea thread so "more" avoids repeats."""
    recipient = ""
    occasion_type = ""
    interest = ""
    if profile:
        recipient = profile.recipient_name or ""
        occasion_id = occasion_id or profile.occasion_id
        occasion_type = profile.occasion_type or ""
        interest = profile.interest or profile.recipient_vibe or ""
        if budget_min is None:
            budget_min = profile.budget_min
        if budget_max is None:
            budget_max = profile.budget_max
    vibe_key = ",".join(sorted(str(v).strip().lower() for v in vibes if str(v).strip()))
    return "|".join(
        str(part).strip().lower()
        for part in (
            scenario,
            occasion_id or "",
            recipient,
            occasion_type,
            recipient_vibe or interest,
            vibe_key,
            budget_min or "",
            budget_max or "",
            budget_text or "",
        )
    )


async def _resolve_contact_for_user(
    db: AsyncSession, user_id: str, name: str,
) -> Contact | None:
    """Find a user's contact by (case-insensitive) name. Most recent wins."""
    norm = " ".join((name or "").strip().lower().split())
    if not norm:
        return None
    rows = (
        await db.execute(
            select(Contact)
            .where(Contact.user_id == user_id)
            .order_by(Contact.created_at.desc())
        )
    ).scalars().all()
    for c in rows:
        if " ".join((c.name or "").strip().lower().split()) == norm:
            return c
    return None


async def _past_gifts_hint(
    db: AsyncSession, phone: str, occasion_id: str | None, profile,
) -> list[dict]:
    """Resolve the recipient (via occasion or profile) and return past gifts.

    Best-effort only — never raises into the suggestion path. Returns [] when
    we can't confidently tie the suggestion to a known contact.
    """
    user = await _get_user(db, phone)
    if not user:
        return []

    contact_id: str | None = None
    occ_id = occasion_id or (getattr(profile, "occasion_id", None) if profile else None)
    if occ_id:
        occ = await db.get(Occasion, occ_id)
        if occ and occ.user_id == user.id and occ.contact_id:
            contact_id = occ.contact_id
    if contact_id is None and profile is not None:
        recipient = getattr(profile, "recipient_name", None)
        if recipient:
            contact = await _resolve_contact_for_user(db, user.id, recipient)
            if contact:
                contact_id = contact.id
    if contact_id is None:
        return []

    return await get_past_gifts_for_contact(db, user.id, contact_id)


async def get_past_gifts_for_contact(
    db: AsyncSession, user_id: str, contact_id: str, limit: int = 10,
) -> list[dict]:
    """Return gifts the user previously logged for a contact, newest first."""
    rows = (
        await db.execute(
            select(GiftLog)
            .where(GiftLog.user_id == user_id, GiftLog.contact_id == contact_id)
            .order_by(GiftLog.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {"description": r.description, "year": r.gift_year}
        for r in rows
    ]


async def log_my_gift(db: AsyncSession, phone: str, args: dict) -> dict:
    """Remember a gift the user gave someone, for future no-repeat hints.

    Off-platform only — no payment or fulfillment. Stores against a contact
    (resolved by name) so suggest_gifts can later avoid repeating it.
    """
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    description = (args.get("description") or "").strip()
    if not description:
        return {"error": "description is required (what did you give?)"}

    recipient_name = (args.get("recipient_name") or "").strip()
    contact_id = (args.get("contact_id") or "").strip() or None
    occasion_id = (args.get("occasion_id") or "").strip() or None

    contact: Contact | None = None
    if contact_id:
        contact = await db.get(Contact, contact_id)
        if contact and contact.user_id != user.id:
            contact = None
    if contact is None and recipient_name:
        contact = await _resolve_contact_for_user(db, user.id, recipient_name)

    gift_year = args.get("gift_year")
    gift_year = str(gift_year).strip()[:4] if gift_year else None

    entry = GiftLog(
        id=generate_id("glog"),
        user_id=user.id,
        contact_id=contact.id if contact else None,
        occasion_id=occasion_id,
        description=description[:200],
        gift_year=gift_year,
    )
    db.add(entry)
    await db.flush()

    who = contact.name if contact else (recipient_name or "them")
    return {
        "success": True,
        "gift_log_id": entry.id,
        "contact_id": contact.id if contact else None,
        "recipient_name": who,
        "description": entry.description,
        "message": f"Noted — I'll remember you gave {who} {entry.description}. 🎁",
    }


def _budget_overlaps(c: GiftCandidate, bmin: int | None, bmax: int | None) -> bool:
    """Candidate overlaps the user's range. Vendor candidates with price 0/0 always pass."""
    return budget_overlaps(c, bmin, bmax)


def _score(
    c: GiftCandidate,
    requested_vibes: list[str],
    bmin: int | None,
    bmax: int | None,
) -> float:
    return score_candidate(c, requested_vibes, bmin, bmax)


async def search_brands(db: AsyncSession, phone: str, args: dict) -> dict:
    """Search available gift card brands by name, category, or budget."""
    query = args.get("query", "").lower()
    budget_max = args.get("budget_max")

    try:
        all_brands = await hubble_service.get_brands()
    except HubbleUnavailableError as e:
        return e.response
    except Exception as e:
        logger.error("Hubble get_brands failed: %s", e)
        return {"error": "Could not fetch brands right now. Try again later."}

    matched = []
    for b in all_brands:
        title = b.get("title", "")
        title_lower = title.lower()
        raw_category = b.get("category", "")
        # Hubble has started returning category as a list of strings for some
        # brands (e.g. ["Fashion", "Lifestyle"]); older rows still return a
        # plain string. Normalize to a single comma-joined string for both
        # matching and the response payload.
        if isinstance(raw_category, list):
            category = ", ".join(str(c) for c in raw_category if c)
        else:
            category = raw_category or ""

        # Filter by query
        if query and query not in title_lower and query not in category.lower():
            continue

        min_amt = b.get("amountRestrictions", {}).get("minAmount", 0)
        max_amt = b.get("amountRestrictions", {}).get("maxAmount", 0)

        # Filter by budget
        if budget_max and min_amt > budget_max:
            continue

        matched.append({
            "id": b["id"],
            "name": title,
            "category": category,
            "price_range": f"₹{min_amt}-₹{max_amt}",
            "min": min_amt,
            "max": max_amt,
        })

    return {
        "brands": matched[:20],
        "total_found": len(matched),
        "showing": min(20, len(matched)),
    }


async def get_brand_details(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get detailed info for a specific brand."""
    brand_name = args.get("brand_name", "").lower()

    try:
        all_brands = await hubble_service.get_brands()
    except HubbleUnavailableError as e:
        return e.response
    except Exception:
        return {"error": "Could not fetch brand details right now."}

    for b in all_brands:
        if brand_name in b.get("title", "").lower():
            restrictions = b.get("amountRestrictions", {})
            denoms = restrictions.get("denominations")
            denom_type = b.get("denominationType", "FLEXIBLE")

            details = {
                "id": b["id"],
                "name": b["title"],
                "category": b.get("category", ""),
                "min_amount": restrictions.get("minAmount", 0),
                "max_amount": restrictions.get("maxAmount", 0),
                "denomination_type": denom_type,
                "description": b.get("description", ""),
            }

            if denoms and denom_type == "FIXED":
                details["available_amounts"] = sorted(denoms)
            else:
                details["flexible"] = True
                details["note"] = f"Any amount between ₹{details['min_amount']} and ₹{details['max_amount']}"

            return details

    return {"error": f"Brand '{args.get('brand_name')}' not found. Try search_brands to see available options."}


async def check_occasion_gifts(db: AsyncSession, phone: str, args: dict) -> dict:
    """Check what gifts have already been bought for an occasion — internal + external.

    Returns a summary the bot can use for the hybrid duplicate-reveal UX:
    - categories_taken / brands_taken lists for privacy-preserving conflict checks
    - total_count so the bot can say "2 gifts already claimed" without revealing specifics
    - items (detailed) only when the bot decides to reveal after a conflict
    """
    occasion_id = args.get("occasion_id")
    if not occasion_id:
        return {"error": "occasion_id is required"}

    # The checklist is the dedup oracle — only the celebrant or invited
    # friends may read it. Otherwise an attacker with a guessed occasion_id
    # could enumerate brands/categories already claimed.
    if await _can_view_occasion_gifts(db, occasion_id, phone) is None:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    checklist = await get_full_gift_checklist(db, occasion_id)
    total = len(checklist["internal"]) + len(checklist["external"])

    if total == 0:
        return {
            "total_count": 0,
            "categories_taken": [],
            "brands_taken": [],
            "message": "Nothing claimed yet — you're the first!",
        }

    return {
        "total_count": total,
        "categories_taken": checklist["categories_taken"],
        "brands_taken": checklist["brands_taken"],
        "internal": checklist["internal"],
        "external": checklist["external"],
        "privacy_note": "Only reveal brand/item names if user's planned gift conflicts. Otherwise keep specifics private.",
    }


async def log_external_purchase(db: AsyncSession, phone: str, args: dict) -> dict:
    """Log a gift a friend bought OUTSIDE Wishkr (Amazon, direct, etc.) against an occasion.

    Used for dedup — so other friends don't duplicate the same gift.
    The celebrant never sees these (surprise preserved).
    """
    occasion_id = args.get("occasion_id")
    item_description = (args.get("item_description") or "").strip()
    category = (args.get("category") or "").strip() or None
    brand_name = (args.get("brand_name") or "").strip() or None
    wishlist_item_id = (args.get("wishlist_item_id") or "").strip() or None

    if not occasion_id:
        return {"error": "occasion_id is required"}
    if not item_description:
        return {"error": "item_description is required (what did you buy?)"}

    # Authorization: only the celebrant or an invited friend may write claim
    # rows. Without this, an attacker can spam ExternalGiftClaim rows on
    # arbitrary occasion_ids and DOS the dedup logic — legitimate gifters
    # would see categories falsely "already claimed".
    if await _can_view_occasion_gifts(db, occasion_id, phone) is None:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    claim = ExternalGiftClaim(
        id=generate_id("ext"),
        occasion_id=occasion_id,
        buyer_phone=phone,
        item_description=item_description,
        category=category.lower() if category else None,
        brand_name=brand_name,
        wishlist_item_id=wishlist_item_id,
    )
    db.add(claim)
    await db.flush()

    # Close the dedupe loop: when the claim names a wishlist item, mark
    # it gifted so suggest_gifts stops surfacing it.
    if wishlist_item_id:
        from app.bot.wishlist_store import mark_gifted
        await mark_gifted(db, wishlist_item_id)

    return {
        "success": True,
        "claim_id": claim.id,
        "message": f"Got it! I've marked '{item_description}' as claimed. Your secret's safe 🤫",
    }


async def suggest_gifts(db: AsyncSession, phone: str, args: dict) -> dict:
    """Recommend gifts through the marketplace recommendation service."""
    scenario = (args.get("scenario") or "for_friend").strip().lower()
    explicit_occasion_id = (args.get("occasion_id") or "").strip() or None
    occasion_id = explicit_occasion_id
    vibes_in = args.get("vibes") or []
    if isinstance(vibes_in, str):
        vibes_in = [v.strip() for v in vibes_in.split(",") if v.strip()]
    recipient_vibe = (args.get("recipient_vibe") or "").strip().lower()
    budget_min = args.get("budget_min")
    budget_max = args.get("budget_max")
    budget_text = (args.get("budget_text") or "").strip()
    desired_categories: list[str] = []
    excluded_categories: list[str] = []
    preferred_brands: list[str] = []
    disliked_brands: list[str] = []
    gift_format: str | None = None
    constraints: dict = {}
    n = int(args.get("n") or 8)
    if n > 20:
        n = 20

    active_state = await get_state(db, phone)
    profile = None
    switched_occasion = False
    if has_active_gift_context(active_state):
        profile = get_gift_need_profile(active_state)
        switched_occasion = bool(
            explicit_occasion_id
            and profile
            and profile.occasion_id != explicit_occasion_id
        )
        if switched_occasion:
            # Protected recipient fields and unprotected budget/interest fields
            # both belong to the previous occasion. Start clean before loading
            # the explicitly requested occasion.
            profile = None
            await clear_state(db, phone, ACTIVE_GIFT_CONTEXT_KEY)
            await clear_state(db, phone, "budget")
        occasion_id = occasion_id or (profile.occasion_id if profile else None)

    from app.services.occasion_gift_plan import (
        get_occasion_gift_plan,
        profile_for_occasion,
        upsert_occasion_gift_plan,
    )

    saved_plan = await get_occasion_gift_plan(db, phone, occasion_id)
    if occasion_id:
        occasion_profile = await profile_for_occasion(
            db,
            phone,
            occasion_id,
            base=profile,
            plan=saved_plan,
        )
        if occasion_profile is None and explicit_occasion_id:
            return _err(
                "occasion_not_found",
                _HINT_OCCASION_UNKNOWN,
                occasion_id=occasion_id,
            )
        profile = occasion_profile or profile

    if saved_plan:
        if budget_min is None:
            budget_min = saved_plan.budget_min
        if budget_max is None:
            budget_max = saved_plan.budget_max
        if not vibes_in and saved_plan.vibe_ids:
            vibes_in = list(saved_plan.vibe_ids)
        if not recipient_vibe and saved_plan.interests:
            recipient_vibe = ", ".join(
                str(value) for value in saved_plan.interests if value
            ).lower()
        desired_categories = list(saved_plan.desired_categories or [])
        excluded_categories = list(saved_plan.excluded_categories or [])
        preferred_brands = list(saved_plan.preferred_brands or [])
        disliked_brands = list(saved_plan.disliked_brands or [])
        gift_format = saved_plan.gift_format
        constraints = dict(saved_plan.constraints or {})

    if profile:
        occasion_id = occasion_id or profile.occasion_id
        if budget_min is None:
            budget_min = profile.budget_min
        if budget_max is None:
            budget_max = profile.budget_max

    for key in ("desired_categories", "excluded_categories", "constraints", "gift_format"):
        if key in args:
            continue
        if profile and getattr(profile, key, None):
            args = {**args, key: getattr(profile, key)}
    desired_categories = list(args.get("desired_categories", desired_categories) or [])
    excluded_categories = list(dict.fromkeys([*excluded_categories, *(args.get("excluded_categories") or [])]))
    constraints = {**constraints, **(args.get("constraints") or {})}
    gift_format = args.get("gift_format") or gift_format
    remembered_budget = active_state.get("budget") or {}
    unlimited = args.get("budget_unlimited")
    if unlimited is None:
        unlimited = False if args.get("budget_min") is not None or args.get("budget_max") is not None else bool(
            (profile and profile.budget_unlimited) or remembered_budget.get("unlimited")
        )
    if unlimited:
        budget_min = budget_max = None
    if "budget_unlimited" in args or args.get("budget_min") is not None or args.get("budget_max") is not None:
        await set_state(db, phone, "budget", {"min": budget_min, "max": budget_max, "unlimited": bool(unlimited)})

    if profile or recipient_vibe or vibes_in or desired_categories or excluded_categories or constraints or unlimited or budget_min is not None or budget_max is not None:
        context_updates: dict[str, object] = {
            **(profile.to_mapping() if profile else {}),
            "desired_categories": desired_categories,
            "excluded_categories": excluded_categories,
            "constraints": constraints,
            "gift_format": gift_format,
            "budget_unlimited": bool(unlimited),
            "current_step": "suggesting_gifts",
        }
        if recipient_vibe:
            context_updates["recipient_vibe"] = recipient_vibe
            context_updates["interest"] = recipient_vibe
        if unlimited or budget_min is not None:
            context_updates["budget_min"] = budget_min
        if unlimited or budget_max is not None:
            context_updates["budget_max"] = budget_max
        await update_active_gift_context(
            db,
            phone,
            context_updates,
            sync_gift_plan=False,
        )
        profile = get_gift_need_profile(await get_state(db, phone))

    history = active_state.get(_SUGGESTION_HISTORY_KEY)
    if not isinstance(history, dict):
        history = {}
    history_key = _suggestion_history_signature(
        scenario=scenario,
        occasion_id=occasion_id,
        vibes=vibes_in,
        recipient_vibe=recipient_vibe,
        budget_min=budget_min,
        budget_max=budget_max,
        budget_text=budget_text,
        profile=profile,
    )
    exclude_names = history.get(history_key)
    if not isinstance(exclude_names, list):
        exclude_names = []
    durable_names = [
        str(item.get("name") or "").strip()
        for item in (
            saved_plan.last_suggestions
            if saved_plan and isinstance(saved_plan.last_suggestions, list)
            else []
        )
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    exclude_names = list(
        dict.fromkeys([*exclude_names, *durable_names])
    )

    result = await recommend_gifts(
        db,
        GiftRecommendationRequest(
            phone=phone,
            profile=profile,
            scenario=scenario,
            occasion_id=occasion_id,
            vibes_in=vibes_in,
            recipient_vibe=recipient_vibe,
            budget_min=budget_min,
            budget_max=budget_max,
            budget_text=budget_text,
            desired_categories=desired_categories,
            excluded_categories=excluded_categories,
            preferred_brands=preferred_brands,
            disliked_brands=disliked_brands,
            gift_format=gift_format,
            constraints=constraints,
            n=n,
            exclude_names=exclude_names,
        ),
        sources=SOURCES,
    )
    visible_suggestions = _user_visible_suggestions(result)
    if result.get("success"):
        shown_names = [
            str(item.get("name") or "").strip()
            for item in visible_suggestions
        ]
        if shown_names:
            history[history_key] = list(dict.fromkeys([*exclude_names, *shown_names]))[-50:]
            await set_state(db, phone, _SUGGESTION_HISTORY_KEY, history)
        past_gifts = await _past_gifts_hint(db, phone, occasion_id, profile)
        if past_gifts:
            result["previously_gifted"] = past_gifts

    review = result.get("recommendation_review")
    review_question = (
        review.get("clarifying_question")
        if isinstance(review, dict)
        else None
    )
    top_level_question = result.get("clarifying_question")
    # The agent prompt gives the refinement question precedence when present,
    # so the repetition guard must track that same user-visible question.
    clarifying_question = (
        review_question
        if isinstance(review_question, str) and review_question.strip()
        else top_level_question
    )
    missing_fields = result.get("missing_fields")
    if isinstance(clarifying_question, str) and clarifying_question.strip():
        clarification_state = {
            "occasion_id": occasion_id,
            "missing_fields": (
                sorted(str(field) for field in missing_fields)
                if isinstance(missing_fields, list)
                else []
            ),
            "question": clarifying_question.strip(),
        }
        if active_state.get(_CLARIFICATION_STATE_KEY) == clarification_state:
            result["clarifying_question"] = None
            if isinstance(review, dict):
                review["clarifying_question"] = None
            result["repeated_question_suppressed"] = True
            result["guidance"] = (
                f"{result.get('guidance', '')} The same clarification was already "
                "asked with no new information. Use any available AI or catalogue "
                "ideas instead of asking it again; if none are available, do not "
                "repeat the question."
            ).strip()
        else:
            await set_state(
                db,
                phone,
                _CLARIFICATION_STATE_KEY,
                clarification_state,
            )
    elif active_state.get(_CLARIFICATION_STATE_KEY):
        await set_state(db, phone, _CLARIFICATION_STATE_KEY, {})

    if occasion_id:
        plan_updates: dict[str, object] = {
            "budget_min": budget_min,
            "budget_max": budget_max,
            "current_step": (
                "suggestions_shown"
                if result.get("success") and visible_suggestions
                else "suggesting_gifts"
            ),
        }
        if recipient_vibe:
            plan_updates["interests"] = [recipient_vibe]
        if vibes_in:
            plan_updates["vibe_ids"] = list(dict.fromkeys(vibes_in))
        if visible_suggestions:
            plan_updates["last_suggestions"] = visible_suggestions[-20:]
        await upsert_occasion_gift_plan(
            db,
            phone,
            occasion_id,
            plan_updates,
            create_if_missing=True,
        )
    return result


async def save_gift_plan(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create or update the caller's private plan for one occasion."""
    occasion_id = (args.get("occasion_id") or "").strip()
    if not occasion_id:
        return {"error": "occasion_id is required"}

    update_fields = {
        "budget_min",
        "budget_max",
        "currency",
        "interests",
        "vibe_ids",
        "desired_categories",
        "excluded_categories",
        "preferred_brands",
        "disliked_brands",
        "gift_format",
        "constraints",
        "recipient_city",
    }
    updates = {
        key: args[key]
        for key in update_fields
        if key in args and args[key] is not None
    }
    if not updates:
        return {
            "error": "gift_plan_updates_required",
            "message": "Pass at least one gift-planning field to save.",
        }
    updates["current_step"] = "gift_plan_saved"

    from app.services.occasion_gift_plan import (
        get_occasion_gift_plan,
        profile_for_occasion,
        upsert_occasion_gift_plan,
    )

    existing_plan = await get_occasion_gift_plan(
        db,
        phone,
        occasion_id,
    )
    for key in ("budget_min", "budget_max"):
        if key not in updates:
            continue
        value = updates[key]
        if isinstance(value, bool):
            return {"error": f"{key} must be a positive rupee amount"}
        try:
            value = int(value)
        except (TypeError, ValueError):
            return {"error": f"{key} must be a positive rupee amount"}
        if value <= 0:
            return {"error": f"{key} must be a positive rupee amount"}
        updates[key] = value
    if "budget_min" in updates or "budget_max" in updates:
        effective_min = updates.get(
            "budget_min",
            existing_plan.budget_min if existing_plan else None,
        )
        effective_max = updates.get(
            "budget_max",
            existing_plan.budget_max if existing_plan else None,
        )
        if (
            effective_min is not None
            and effective_max is not None
            and effective_min > effective_max
        ):
            return {"error": "budget_min cannot exceed budget_max"}
    if "currency" in updates:
        currency = str(updates["currency"]).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            return {"error": "currency must be a three-letter ISO code"}
        updates["currency"] = currency
    if (
        updates.get("gift_format") is not None
        and updates["gift_format"] not in {"physical", "digital", "experience", "any"}
    ):
        return {
            "error": "gift_format must be physical, digital, experience, or any"
        }

    plan = await upsert_occasion_gift_plan(
        db,
        phone,
        occasion_id,
        updates,
        create_if_missing=True,
    )
    if plan is None:
        return _err(
            "occasion_not_found",
            _HINT_OCCASION_UNKNOWN,
            occasion_id=occasion_id,
        )

    profile = await profile_for_occasion(
        db,
        phone,
        occasion_id,
        plan=plan,
    )
    if profile:
        state = await get_state(db, phone)
        active = get_gift_need_profile(state)
        if active and active.occasion_id != occasion_id:
            await clear_state(db, phone, ACTIVE_GIFT_CONTEXT_KEY)
            await clear_state(db, phone, "budget")
        context_updates: dict[str, object] = {
            **profile.to_mapping(),
            "_explicit_context_change": True,
            "current_step": plan.current_step or "gift_plan_saved",
        }
        await update_active_gift_context(
            db,
            phone,
            context_updates,
            sync_gift_plan=False,
        )

    return {
        "success": True,
        "occasion_id": occasion_id,
        "gift_plan": {
            "budget_min": plan.budget_min,
            "budget_max": plan.budget_max,
            "currency": plan.currency,
            "interests": plan.interests or [],
            "vibe_ids": plan.vibe_ids or [],
            "desired_categories": plan.desired_categories or [],
            "excluded_categories": plan.excluded_categories or [],
            "preferred_brands": plan.preferred_brands or [],
            "disliked_brands": plan.disliked_brands or [],
            "gift_format": plan.gift_format,
            "constraints": plan.constraints or {},
            "recipient_city": plan.recipient_city,
        },
    }


async def suggest_marketplace_gift_card(db: AsyncSession, phone: str, args: dict) -> dict:
    """Find an online-marketplace voucher (Amazon, Flipkart, Myntra, …) the
    user can send when they ask for product-level ideas from a site we
    don't integrate.

    Behaviour: query gift_catalog for `is_marketplace=True` rows matching
    the requested name (substring, lowercased). Return the top-N with
    denominations so the agent can pivot from "suggest something from
    Amazon" → "I can send a ₹500–₹10,000 Amazon voucher — they pick".
    """
    marketplace = (args.get("marketplace") or "").strip().lower()
    if len(marketplace) < 2:
        return {"error": "marketplace_required", "message": "Pass a marketplace name (amazon, flipkart, myntra, etc.)."}

    n = int(args.get("n") or 3)
    if n > 10:
        n = 10

    from app.models.gift_catalog import GiftCatalogItem

    result = await db.execute(
        select(GiftCatalogItem)
        .where(
            GiftCatalogItem.active.is_(True),
            GiftCatalogItem.is_marketplace.is_(True),
            GiftCatalogItem.brand_name_lower.contains(marketplace),
        )
        .limit(n)
    )
    items = list(result.scalars().all())

    suggestions: list[dict] = []
    if items:
        suggestions = [
            {
                "brand_name": it.brand_name,
                "item_id": it.external_id,
                "min_amount": it.min_amount,
                "max_amount": it.max_amount,
                "image_url": it.image_url,
                "why": f"{it.brand_name} voucher — recipient picks any product on {marketplace}",
            }
            for it in items
        ]
    else:
        # gift_catalog can lag the live Hubble brand list (catalog sync runs
        # on a schedule). Don't tell the user "we don't stock it" if Hubble
        # actually carries an Amazon/Flipkart/etc. brand right now — fall
        # back to the live brand list.
        try:
            live_brands = await hubble_service.get_brands()
        except HubbleUnavailableError:
            live_brands = []
        except Exception as e:
            logger.warning("hubble fallback failed in suggest_marketplace_gift_card: %s", e)
            live_brands = []
        for b in live_brands:
            title = (b.get("title") or "").strip()
            if not title or marketplace not in title.lower():
                continue
            amount = b.get("amountRestrictions") or {}
            suggestions.append({
                "brand_name": title,
                "item_id": str(b.get("id") or ""),
                "min_amount": amount.get("minAmount") or 0,
                "max_amount": amount.get("maxAmount") or 0,
                "image_url": b.get("imageUrl") or b.get("image_url"),
                "why": f"{title} voucher — recipient picks any product on {marketplace}",
            })
            if len(suggestions) >= n:
                break

    if not suggestions:
        return {
            "success": True,
            "marketplace": marketplace,
            "found": False,
            "message": (
                f"We don't currently stock a {marketplace} voucher. "
                "Offer another marketplace (Amazon, Flipkart) or a category-specific gift card instead."
            ),
            "suggestions": [],
        }
    return {
        "success": True,
        "marketplace": marketplace,
        "found": True,
        "suggestions": suggestions,
        "guidance": (
            "Offer the voucher as a pivot: Wishkr doesn't browse marketplace products, "
            "but a gift card lets the recipient pick exactly what they want. "
            "Use create_gift_order with the brand_name + amount once they confirm."
        ),
    }


async def get_self_preferences(db: AsyncSession, phone: str, args: dict) -> dict:
    """Return the current user's own gift preferences + wishlists across their celebrant occasions.

    Use BEFORE calling suggest_gifts when the user is asking for ideas FOR THEMSELVES.
    If preferences exist, pass them as `vibes` to suggest_gifts. If empty, ask the user
    once for 1-3 categories before suggesting.
    """
    from app.models import CelebrantOccasion
    user = await _get_user(db, phone)
    if not user:
        return {"has_preferences": False, "preferences": [], "wishlists": [], "occasions": []}

    # Find the user's own occasions that have a celebrant_occasion row
    own_occ_q = (
        select(Occasion).where(
            Occasion.user_id == user.id, Occasion.contact_id.is_(None),
            Occasion.deleted_at.is_(None),
        )
    )
    own_occasions = (await db.execute(own_occ_q)).scalars().all()
    if not own_occasions:
        return {"has_preferences": False, "preferences": [], "wishlists": [], "occasions": []}

    occ_ids = [o.id for o in own_occasions]
    cel_q = select(CelebrantOccasion).where(CelebrantOccasion.occasion_id.in_(occ_ids))
    cel_occs = (await db.execute(cel_q)).scalars().all()

    all_prefs: list[str] = []
    all_wishlists: list[dict] = []
    occasions_summary: list[dict] = []
    for co in cel_occs:
        all_prefs.extend(co.preferences_list)
        all_wishlists.extend(co.wishlists)
        occ = next((o for o in own_occasions if o.id == co.occasion_id), None)
        if occ:
            occasions_summary.append({
                "occasion_id": occ.id,
                "celebrant_occasion_id": co.id,
                "type": occ.type.value if occ.type else "",
                "date": occ.date.strftime("%d %B %Y") if occ.date else "",
                "preferences": co.preferences_list,
            })

    # Dedupe preserving order
    seen = set()
    unique_prefs = []
    for p in all_prefs:
        pl = p.strip()
        if pl and pl.lower() not in seen:
            seen.add(pl.lower())
            unique_prefs.append(pl)

    return {
        "has_preferences": bool(unique_prefs),
        "preferences": unique_prefs,
        "wishlists": all_wishlists,
        "occasions": occasions_summary,
        "guidance": (
            "Pass these preferences as `vibes` (or join them as recipient_vibe) "
            "into suggest_gifts. If empty, ask the user once for 1-3 categories."
        ) if unique_prefs else (
            "User has no saved gift preferences for their own occasions. "
            "Ask them once for 1-3 categories (e.g. gadgets, fashion, books) "
            "before calling suggest_gifts."
        ),
    }
