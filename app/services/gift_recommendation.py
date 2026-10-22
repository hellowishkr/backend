"""Deterministic gift recommendation service.

The LLM extracts intent and rough slots; this service owns marketplace fan-out,
provider policy checks, budget fit, dedupe, and ranking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_context import GiftNeedProfile
from app.bot.gift_taxonomy import all_vibe_ids, default_vibes_for_occasion
from app.bot.language_map import resolve_vibe_keywords
from app.bot.sources import GiftCandidate, SourceAdapter, enabled_sources
from app.core.config import settings
from app.models import Contact, Occasion, WishlistItem
from app.utils.budget_parser import (
    budget_ranges_overlap,
    parse_budget,
    parse_budget_hint,
)
from app.utils.dedup import get_full_gift_checklist
from app.utils.text_matching import count_match_values, normalize_match_values

logger = logging.getLogger("wishkr.recommendations")

FULFILLMENT_BONUS: dict[str, float] = {
    "instant_voucher": 2.0,
    "physical_delivery": 1.0,
    "experience_voucher": 1.0,
    "booking": 0.0,
}

_IDEA_LIBRARY: dict[str, list[dict[str, str]]] = {
    "gen_z_streetwear": [
        {
            "name": "Oversized cotton T-shirt",
            "budget_hint": "around ₹600-₹1,200",
            "why": "comfortable and personal when you know their size and style",
        },
        {
            "name": "Soft everyday hoodie",
            "budget_hint": "around ₹1,200-₹2,500",
            "why": "a cosy option for someone who likes relaxed clothing",
        },
        {
            "name": "Comfy lounge set",
            "budget_hint": "around ₹1,000-₹2,000",
            "why": "useful for everyday wear if you know their size",
        },
    ],
    "premium_western": [
        {
            "name": "Relaxed-fit shirt",
            "budget_hint": "around ₹900-₹1,800",
            "why": "comfortable but a little more polished than loungewear",
        },
    ],
    "art_supplies": [
        {
            "name": "Sketchbook and brush-pen set",
            "budget_hint": "around ₹500-₹1,200",
            "why": "a safe art gift when you are unsure which medium they prefer",
        },
        {
            "name": "Acrylic marker set",
            "budget_hint": "around ₹700-₹1,500",
            "why": "fun for illustration, journaling, and small craft projects",
        },
        {
            "name": "Mini canvas and acrylic-paint kit",
            "budget_hint": "around ₹600-₹1,400",
            "why": "a complete creative activity rather than a single supply",
        },
    ],
    "wall_art": [
        {"name": "Framed art print", "budget_hint": "around ₹500-₹1,500", "why": "a finished piece for their home; choose a style that suits the room"},
        {"name": "Personalized illustrated portrait", "budget_hint": "around ₹1,000-₹3,000", "why": "a personal keepsake to display"},
        {"name": "Small framed original artwork", "budget_hint": "around ₹1,500-₹3,000", "why": "a finished artwork rather than a painting kit"},
    ],
}


@dataclass(slots=True)
class GiftRecommendationRequest:
    phone: str
    profile: GiftNeedProfile | None = None
    scenario: str = "for_friend"
    occasion_id: str | None = None
    vibes_in: list[str] | None = None
    recipient_vibe: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    budget_text: str | None = None
    desired_categories: list[str] | None = None
    excluded_categories: list[str] | None = None
    preferred_brands: list[str] | None = None
    disliked_brands: list[str] | None = None
    gift_format: str | None = None
    constraints: dict | None = None
    n: int = 8
    exclude_names: list[str] | None = None


def _constraint_values(constraints: dict | None) -> list[str]:
    if not isinstance(constraints, dict):
        return []
    return normalize_match_values(
        [str(value) for value in constraints.values() if value is not None and str(value).strip()]
    )


def _candidate_text(candidate: GiftCandidate) -> str:
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    return " ".join(
        str(value)
        for value in (
            candidate.name,
            metadata.get("category", ""),
            " ".join(candidate.vibes or []),
        )
        if value
    ).lower()


def _candidate_gift_format(candidate: GiftCandidate) -> str:
    if candidate.fulfillment in {"instant_voucher"}:
        return "digital"
    if candidate.fulfillment in {"experience_voucher", "booking"}:
        return "experience"
    return "physical"


def _candidate_allowed(
    candidate: GiftCandidate,
    request: GiftRecommendationRequest,
) -> bool:
    known_vibes = set(all_vibe_ids())
    excluded = normalize_match_values(request.excluded_categories)
    excluded_vibes = resolve_vibe_keywords(excluded, known_vibes)
    candidate_vibes = set(candidate.vibes or [])
    text = _candidate_text(candidate)
    desired = normalize_match_values(request.desired_categories)
    if desired and not count_match_values(text, desired):
        desired_vibes = resolve_vibe_keywords(desired, known_vibes)
        if not candidate_vibes.intersection(desired_vibes):
            return False
    if candidate_vibes & excluded_vibes:
        return False
    if count_match_values(text, excluded):
        return False

    disliked = normalize_match_values(request.disliked_brands)
    candidate_name = " ".join(candidate.name.strip().lower().split())
    if count_match_values(candidate_name, disliked):
        return False

    gift_format = (request.gift_format or "any").strip().lower()
    if (
        gift_format in {"physical", "digital", "experience"}
        and _candidate_gift_format(candidate) != gift_format
    ):
        return False
    if not budget_overlaps(
        candidate,
        request.budget_min,
        request.budget_max,
    ):
        return False
    return True


def budget_overlaps(c: GiftCandidate, bmin: int | None, bmax: int | None) -> bool:
    if c.price_min == 0 and c.price_max == 0:
        return True
    return budget_ranges_overlap(c.price_min, c.price_max, bmin, bmax)


def score_candidate(
    c: GiftCandidate,
    requested_vibes: list[str],
    bmin: int | None,
    bmax: int | None,
    *,
    preferred_brands: list[str] | None = None,
    desired_categories: list[str] | None = None,
    constraints: dict | None = None,
) -> float:
    matched = len(set(c.vibes) & set(requested_vibes))
    score = matched * 10.0
    if budget_overlaps(c, bmin, bmax):
        score += 5.0
    else:
        score -= 100.0
    if c.already_taken:
        score -= 3.0
    if c.policy and not c.policy.serviceable:
        score -= 100.0
    candidate_text = _candidate_text(c)
    score += 4.0 * count_match_values(
        c.name,
        normalize_match_values(preferred_brands),
    )
    score += 2.0 * count_match_values(
        candidate_text,
        normalize_match_values(desired_categories),
    )
    score += min(
        3,
        count_match_values(candidate_text, _constraint_values(constraints)),
    )
    score += FULFILLMENT_BONUS.get(c.fulfillment, 0.0)
    score += float(getattr(c.policy, "reliability_score", 0.5) or 0.5)
    return score


async def resolve_vibes_for_request(
    request: GiftRecommendationRequest,
    *,
    occasion_type: str | None = None,
) -> list[str]:
    known_vibes = set(all_vibe_ids())
    vibes_set: set[str] = set()
    raw_vibes = request.vibes_in or []
    vibes_set |= resolve_vibe_keywords(raw_vibes, known_vibes)
    vibes_set |= resolve_vibe_keywords(
        request.desired_categories or [],
        known_vibes,
    )
    if request.recipient_vibe:
        vibes_set |= resolve_vibe_keywords([request.recipient_vibe], known_vibes)
        vibes_set |= resolve_vibe_keywords(request.recipient_vibe.split(), known_vibes)
    if request.profile:
        saved_descriptions = [
            request.profile.recipient_vibe,
            request.profile.interest,
            request.profile.gift_category,
        ]
        for saved in saved_descriptions:
            if saved:
                vibes_set |= resolve_vibe_keywords([saved], known_vibes)
                vibes_set |= resolve_vibe_keywords(saved.split(), known_vibes)

    if not vibes_set:
        from app.bot.vibe_embedder import resolve as _embed_resolve

        query_text = " ".join(
            str(bit) for bit in [request.recipient_vibe, " ".join(raw_vibes)] if bit
        ).strip()
        if query_text:
            try:
                semantic_vibes = await _embed_resolve(query_text)
                vibes_set.update(v for v in semantic_vibes if v in known_vibes)
            except Exception:
                logger.exception("vibe_embedder.resolve crashed; ignoring")

    if not vibes_set and occasion_type:
        vibes_set.update(default_vibes_for_occasion(occasion_type))
    excluded_vibes = resolve_vibe_keywords(
        request.excluded_categories or [],
        known_vibes,
    )
    vibes_set.difference_update(excluded_vibes)
    if not vibes_set and occasion_type:
        vibes_set.update(default_vibes_for_occasion(occasion_type))
        vibes_set.difference_update(excluded_vibes)
    return list(vibes_set)


def _idea_suggestions(
    vibes: list[str],
    n: int,
    *,
    budget_min: int | None = None,
    budget_max: int | None = None,
    excluded_categories: list[str] | None = None,
    gift_format: str | None = None,
    constraints: dict | None = None,
    recipient_interest: str | None = None,
) -> list[dict]:
    """Return catalogue-independent ideas for reminder-only planning.

    These are general gift concepts, not product inventory or availability
    claims. Interleaving keeps multi-interest requests balanced.
    """
    known_vibes = set(all_vibe_ids())
    excluded = normalize_match_values(excluded_categories)
    excluded_vibes = resolve_vibe_keywords(excluded, known_vibes)
    requested_format = (gift_format or "any").strip().lower()
    if requested_format in {"digital", "experience"}:
        return []
    constraint_values = _constraint_values(constraints)
    groups: list[tuple[str, list[dict[str, str]]]] = []
    for vibe in vibes:
        if vibe not in _IDEA_LIBRARY or vibe in excluded_vibes:
            continue
        filtered = []
        library_key = vibe
        if vibe == "wall_art" and any(word in (recipient_interest or "").lower() for word in ("art supplies", "sketchbook", "painting kit", "paint supplies")):
            library_key = "art_supplies"
        for idea in _IDEA_LIBRARY[library_key]:
            idea_min, idea_max = parse_budget_hint(idea.get("budget_hint"))
            idea_text = " ".join(
                (
                    vibe,
                    idea.get("name", ""),
                    idea.get("why", ""),
                )
            ).lower()
            if not budget_ranges_overlap(
                idea_min,
                idea_max,
                budget_min,
                budget_max,
            ):
                continue
            if count_match_values(idea_text, excluded):
                continue
            filtered.append(idea)
        filtered.sort(
            key=lambda idea: count_match_values(
                " ".join(
                    (
                        vibe,
                        idea.get("name", ""),
                        idea.get("why", ""),
                    )
                ),
                constraint_values,
            ),
            reverse=True,
        )
        if filtered:
            groups.append((vibe, filtered))
    if not groups:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    max_len = max(len(group) for _, group in groups)
    for index in range(max_len):
        for vibe, group in groups:
            if index >= len(group):
                continue
            idea = group[index]
            key = idea["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": idea["name"],
                    "vibes": [vibe],
                    "why": idea["why"],
                    "already_taken": False,
                    "budget_hint": idea["budget_hint"],
                    "availability": "general_idea",
                }
            )
            if len(out) >= n:
                return out
    return out


def _recommendation_context(
    request: GiftRecommendationRequest,
    *,
    occasion_type: str | None,
    vibes: list[str],
    budget_min: int | None,
    budget_max: int | None,
) -> tuple[dict, list[str], str | None]:
    """Describe known/missing slots so the agent never repeats known questions."""
    profile = request.profile
    known_fields: dict[str, object] = {"vibes": vibes}
    if occasion_type:
        known_fields["occasion_type"] = occasion_type
    if budget_min is not None:
        known_fields["budget_min"] = budget_min
    if budget_max is not None:
        known_fields["budget_max"] = budget_max
    if profile and profile.recipient_name:
        known_fields["recipient_name"] = profile.recipient_name

    occasion_defaults = set(default_vibes_for_occasion(occasion_type or ""))
    requested_vibes = {
        str(value).strip() for value in (request.vibes_in or []) if str(value).strip()
    }
    has_recipient_interest = bool(
        request.recipient_vibe
        or (profile and (profile.recipient_vibe or profile.interest or profile.gift_category))
        or (requested_vibes - occasion_defaults)
    )

    missing_fields: list[str] = []
    if budget_min is None and budget_max is None and not (profile and profile.budget_unlimited):
        missing_fields.append("budget")
    if not has_recipient_interest:
        missing_fields.append("recipient_interests")

    recipient = profile.recipient_name if profile and profile.recipient_name else "the recipient"
    if "budget" in missing_fields:
        clarifying_question = "What budget would you like to stay within?"
    elif "recipient_interests" in missing_fields:
        clarifying_question = f"What does {recipient} usually enjoy?"
    else:
        clarifying_question = None
    return known_fields, missing_fields, clarifying_question


def _materialize_refinement_suggestions(result: dict) -> None:
    """Promote validated AI output only when the deterministic list was empty."""
    if result.get("suggestions"):
        return
    review = result.get("recommendation_review")
    if not isinstance(review, dict):
        return

    if review.get("wishkr_catalogue_match_found"):
        catalogue_options = review.get("catalogue_options")
        if isinstance(catalogue_options, list) and catalogue_options:
            result["suggestions"] = [
                {**item, "availability": "wishkr_catalogue"}
                for item in catalogue_options
                if isinstance(item, dict)
            ]
            result["mode"] = "wishkr_catalogue"
            return

    general_ideas = review.get("additional_general_ideas")
    if isinstance(general_ideas, list) and general_ideas:
        result["suggestions"] = [item for item in general_ideas if isinstance(item, dict)]
        result["mode"] = "ai_general_ideas"


def _coerce_budget(value) -> int | None:
    if value is None or isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _budget_hint(candidate: GiftCandidate) -> str:
    if candidate.price_min and candidate.price_max:
        if candidate.price_min == candidate.price_max:
            return f"around ₹{candidate.price_min}"
        return f"around ₹{candidate.price_min}-₹{candidate.price_max}"
    if candidate.price_min:
        return f"from ₹{candidate.price_min}"
    return "budget varies"


async def _attach_refinement(
    db: AsyncSession,
    request: GiftRecommendationRequest,
    result: dict,
    *,
    occasion_id: str | None,
    budget_min: int | None,
    budget_max: int | None,
) -> dict:
    """Attach an optional AI review without weakening deterministic fallback."""
    if not settings.gift_refinement_enabled:
        return result

    from app.services.gift_refinement import (
        GiftRefinementRequest,
        refine_gift_recommendations,
    )

    try:
        review = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone=request.phone,
                suggestions=result.get("suggestions") or [],
                profile=request.profile,
                occasion_id=occasion_id,
                budget_min=budget_min,
                budget_max=budget_max,
                budget_text=request.budget_text,
                desired_categories=request.desired_categories,
                excluded_categories=request.excluded_categories,
                preferred_brands=request.preferred_brands,
                disliked_brands=request.disliked_brands,
                gift_format=request.gift_format,
                constraints=request.constraints,
                exclude_names=request.exclude_names,
            ),
        )
    except Exception:
        logger.exception("Gift refinement crashed; returning deterministic suggestions")
        return result
    if review:
        result["recommendation_review"] = review
    return result


async def _recipient_city(
    db: AsyncSession,
    phone: str,
    occasion_id: str | None,
    profile: GiftNeedProfile | None,
) -> str | None:
    if profile and profile.recipient_city:
        return profile.recipient_city
    from app.utils.phones import user_for_phone

    user = await user_for_phone(db, phone)
    city_id = user.city_id if user else None
    if occasion_id:
        occ = await db.get(Occasion, occasion_id)
        if occ and occ.contact_id:
            contact = await db.get(Contact, occ.contact_id)
            if contact and contact.city_id:
                city_id = contact.city_id
    return city_id


def _delivery_feasible(
    candidate: GiftCandidate, occasion_date: str | None
) -> tuple[bool, str | None]:
    lead_hours = candidate.policy.lead_time_hours if candidate.policy else None
    if not lead_hours or not occasion_date:
        return True, None
    try:
        due = datetime.fromisoformat(occasion_date)
    except ValueError:
        return True, None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    hours_left = (due - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours_left < lead_hours:
        return False, f"Needs about {lead_hours} hours lead time."
    return True, None


async def _wishlist_signal_count(
    db: AsyncSession,
    occasion_id: str | None,
) -> int:
    if not occasion_id:
        return 0
    rows = await db.execute(
        select(WishlistItem.id).where(
            WishlistItem.recipient_occasion_id == occasion_id,
            WishlistItem.purpose == "gift",
            WishlistItem.gifted_at.is_(None),
            WishlistItem.unavailable_at.is_(None),
        )
    )
    return len(rows.all())


async def recommend_gifts(
    db: AsyncSession,
    request: GiftRecommendationRequest,
    *,
    sources: Iterable[SourceAdapter] | None = None,
) -> dict:
    profile = request.profile
    occasion_id = request.occasion_id or (profile.occasion_id if profile else None)
    budget_min = _coerce_budget(request.budget_min)
    budget_max = _coerce_budget(request.budget_max)
    if request.budget_text and budget_min is None and budget_max is None:
        budget_min, budget_max = parse_budget(request.budget_text)
    # Downstream hard filters receive the request object, so replace any raw
    # strings/text-only budget with the canonical integer bounds once.
    request.budget_min = budget_min
    request.budget_max = budget_max

    occasion_type: str | None = None
    if occasion_id:
        occ = await db.get(Occasion, occasion_id)
        if occ and occ.type:
            occasion_type = occ.type.value
    if not occasion_type and profile:
        occasion_type = profile.occasion_type

    vibes = await resolve_vibes_for_request(request, occasion_type=occasion_type)
    if not vibes:
        return {
            "error": "no_vibes_resolved",
            "user_message": "Tell me a little about what they like, and I’ll suggest better gifts.",
            "message": "Couldn't infer a vibe. Pass an explicit `vibes` list or describe the recipient (recipient_vibe).",
            "available_vibes_sample": sorted(all_vibe_ids())[:20],
        }

    known_fields, missing_fields, clarifying_question = _recommendation_context(
        request,
        occasion_type=occasion_type,
        vibes=vibes,
        budget_min=budget_min,
        budget_max=budget_max,
    )
    recommendation_context = {
        "known_fields": known_fields,
        "missing_fields": missing_fields,
        "clarifying_question": clarifying_question,
    }

    city_id = await _recipient_city(db, request.phone, occasion_id, profile)
    already_brands: set[str] = set()
    if occasion_id:
        try:
            checklist = await get_full_gift_checklist(db, occasion_id)
            already_brands = {b.lower() for b in checklist.get("brands_taken", [])}
        except Exception as e:
            logger.warning("Dedupe lookup failed for %s: %s", occasion_id, e)

    all_candidates: list[GiftCandidate] = []
    active_sources = list(sources) if sources is not None else enabled_sources()
    for source in active_sources:
        try:
            candidates = await source.list_candidates(
                db=db,
                vibes=vibes,
                budget_min=budget_min,
                budget_max=budget_max,
                city=city_id,
            )
            all_candidates.extend(candidates)
        except Exception as e:
            logger.warning("Source %s failed: %s", source.source_id, e)

    for candidate in all_candidates:
        if candidate.name.lower() in already_brands:
            candidate.already_taken = True
        feasible, reason = _delivery_feasible(
            candidate,
            profile.occasion_date if profile else None,
        )
        if not feasible:
            candidate.policy.serviceable = False
            candidate.policy.serviceability_reason = reason

    eligible_candidates = [
        candidate for candidate in all_candidates if _candidate_allowed(candidate, request)
    ]
    scored = sorted(
        eligible_candidates,
        key=lambda c: score_candidate(
            c,
            vibes,
            budget_min,
            budget_max,
            preferred_brands=request.preferred_brands,
            desired_categories=request.desired_categories,
            constraints=request.constraints,
        ),
        reverse=True,
    )
    exclude_names = {
        str(name).strip().lower() for name in (request.exclude_names or []) if str(name).strip()
    }
    fresh_scored = [c for c in scored if c.name.strip().lower() not in exclude_names]
    pool = fresh_scored if exclude_names else scored
    top = pool[: max(1, min(int(request.n or 8), 20))]
    suggestions = []
    for candidate in top:
        matched = sorted(set(candidate.vibes) & set(vibes))
        why_parts = []
        if matched:
            why_parts.append(f"matches {', '.join(matched)}")
        if not settings.mvp_reminder_only:
            if candidate.fulfillment == "instant_voucher":
                why_parts.append("instant delivery")
            elif candidate.fulfillment == "physical_delivery":
                why_parts.append(
                    "same-day express delivery"
                    if candidate.policy.express_delivery_available_today
                    else "express delivery"
                    if candidate.policy.express_delivery
                    else "delivered to door"
                )
            elif candidate.fulfillment == "booking":
                why_parts.append("booking-based")
        elif matched:
            why_parts.append("popular for this interest")
        if candidate.already_taken:
            why_parts.append("already claimed for this occasion")
        if candidate.policy.serviceability_reason:
            why_parts.append(candidate.policy.serviceability_reason)

        price_range = (
            f"₹{candidate.price_min}-₹{candidate.price_max}"
            if candidate.price_max and candidate.price_min
            else "ask partner"
            if candidate.policy.fulfillment_mode == "manual"
            else f"from ₹{candidate.price_min}"
            if candidate.price_min
            else "varies"
        )
        suggestions.append(
            {
                "name": candidate.name,
                "vibes": candidate.vibes,
                "why": "; ".join(why_parts),
                "already_taken": candidate.already_taken,
                "budget_hint" if settings.mvp_reminder_only else "price_range": (
                    _budget_hint(candidate) if settings.mvp_reminder_only else price_range
                ),
                **(
                    {}
                    if settings.mvp_reminder_only
                    else {
                        "source": candidate.source_id,
                        "provider_id": candidate.source_id,
                        "fulfillment": candidate.fulfillment,
                        "fulfillment_mode": candidate.policy.fulfillment_mode,
                        "serviceable": candidate.policy.serviceable,
                        "serviceability_reason": candidate.policy.serviceability_reason,
                        "delivery_policy": candidate.policy.delivery_policy,
                        "express_delivery": candidate.policy.express_delivery,
                        "express_delivery_available_today": candidate.policy.express_delivery_available_today,
                        "express_delivery_hours": candidate.policy.lead_time_hours,
                        "express_delivery_cutoff_hour": candidate.policy.same_day_cutoff_hour,
                        "return_policy": candidate.policy.return_policy,
                        "cancellation_policy": candidate.policy.cancellation_policy,
                        "item_id": candidate.item_id,
                        "contact_info": candidate.contact_info,
                    }
                ),
            }
        )

    if settings.mvp_reminder_only:
        seen_names = {item["name"].strip().lower() for item in suggestions}
        for idea in _idea_suggestions(
            sorted(resolve_vibe_keywords(request.desired_categories, set(all_vibe_ids())))
            if request.desired_categories else vibes,
            max(1, min(int(request.n or 8), 20)),
            budget_min=budget_min,
            budget_max=budget_max,
            excluded_categories=request.excluded_categories,
            gift_format=request.gift_format,
            constraints=request.constraints,
            recipient_interest=request.recipient_vibe or (
                (request.profile.recipient_vibe or request.profile.interest) if request.profile else None
            ),
        ):
            idea_name = idea["name"].strip().lower()
            if idea_name in seen_names or idea_name in exclude_names:
                continue
            suggestions.append(idea)
            seen_names.add(idea_name)
            if len(suggestions) >= max(1, min(int(request.n or 8), 20)):
                break

        if not suggestions:
            result = {
                "success": True,
                "mode": "ai_fallback_pending",
                "suggestions": [],
                **recommendation_context,
                "message": "I couldn't find a suitable catalog match for those requirements.",
                "guidance": (
                    "No deterministic match was found. Use the attached AI-generated "
                    "ideas when present. Never ask for a field listed in known_fields, "
                    "and do not claim a general idea is stocked by Wishkr."
                ),
                "vibes_used": vibes,
            }
            result = await _attach_refinement(
                db,
                request,
                result,
                occasion_id=occasion_id,
                budget_min=budget_min,
                budget_max=budget_max,
            )
            _materialize_refinement_suggestions(result)
            if result["suggestions"]:
                result["suggestions"] = [
                    item
                    for item in result["suggestions"]
                    if (
                        isinstance(item, dict)
                        and str(item.get("name") or "").strip().lower() not in exclude_names
                    )
                ]
            return result

        result = {
            "success": True,
            "mode": "idea_only",
            "vibes_used": vibes,
            "suggestions": suggestions,
            **recommendation_context,
            "message": (
                "Use these practical ideas to help the user compare options. "
                "Do not say nothing was found and never ask for a field already "
                "listed in known_fields. Ask at most the supplied clarifying_question."
            ),
        }
        return await _attach_refinement(
            db,
            request,
            result,
            occasion_id=occasion_id,
            budget_min=budget_min,
            budget_max=budget_max,
        )

    result = {
        "success": True,
        "scenario": request.scenario,
        "vibes_used": vibes,
        "occasion_type": occasion_type,
        "recipient_city": city_id,
        "wishlist_signal_count": await _wishlist_signal_count(db, occasion_id),
        "total_candidates": len(all_candidates),
        "eligible_candidates": len(eligible_candidates),
        "showing": len(suggestions),
        "suggestions": suggestions,
        **recommendation_context,
        "note": (
            "Suggestions are ranked by recipient fit, budget, provider policy, "
            "serviceability, and fulfillment mode. Manual vendors require Wishkr confirmation."
        ),
    }
    return await _attach_refinement(
        db,
        request,
        result,
        occasion_id=occasion_id,
        budget_min=budget_min,
        budget_max=budget_max,
    )


class GiftRecommendationService:
    """Marketplace recommendation facade for tools and future checkout flows."""

    def __init__(self, sources: Iterable[SourceAdapter] | None = None):
        self._sources = list(sources) if sources is not None else None

    async def recommend(
        self,
        db: AsyncSession,
        request: GiftRecommendationRequest,
    ) -> dict:
        return await recommend_gifts(db, request, sources=self._sources)
