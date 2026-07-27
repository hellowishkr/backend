"""Persistence helpers for giver-owned, per-occasion gift planning."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_context import GiftNeedProfile
from app.models import (
    CelebrantFriend,
    CelebrantOccasion,
    Contact,
    Occasion,
    OccasionGiftPlan,
    User,
)
from app.utils.id_gen import generate_id

_PLAN_FIELDS = {
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
    "selected_candidate_id",
    "selected_provider_id",
    "selected_brand",
    "current_step",
    "last_suggestions",
}
_LIST_FIELDS = {
    "interests",
    "vibe_ids",
    "desired_categories",
    "excluded_categories",
    "preferred_brands",
    "disliked_brands",
    "last_suggestions",
}


async def _resolve_occasion(
    db: AsyncSession,
    occasion_reference: str,
) -> Occasion | None:
    occasion = await db.get(Occasion, occasion_reference)
    if occasion:
        return occasion
    celebrant = await db.get(CelebrantOccasion, occasion_reference)
    return await db.get(Occasion, celebrant.occasion_id) if celebrant else None


async def _authorized_user_and_occasion(
    db: AsyncSession,
    phone: str,
    occasion_reference: str,
) -> tuple[User, Occasion] | None:
    user = (
        await db.execute(select(User).where(User.phone == phone))
    ).scalar_one_or_none()
    if not user:
        return None
    occasion = await _resolve_occasion(db, occasion_reference)
    if not occasion:
        return None
    if occasion.user_id == user.id:
        return user, occasion

    invited = (
        await db.execute(
            select(CelebrantFriend.id)
            .join(
                CelebrantOccasion,
                CelebrantFriend.celebrant_occasion_id == CelebrantOccasion.id,
            )
            .where(
                CelebrantOccasion.occasion_id == occasion.id,
                or_(
                    CelebrantFriend.user_id == user.id,
                    CelebrantFriend.phone == phone,
                ),
            )
        )
    ).first()
    return (user, occasion) if invited else None


async def get_occasion_gift_plan(
    db: AsyncSession,
    phone: str,
    occasion_reference: str | None,
) -> OccasionGiftPlan | None:
    if not occasion_reference:
        return None
    authorized = await _authorized_user_and_occasion(db, phone, occasion_reference)
    if not authorized:
        return None
    user, occasion = authorized
    return (
        await db.execute(
            select(OccasionGiftPlan).where(
                OccasionGiftPlan.user_id == user.id,
                OccasionGiftPlan.occasion_id == occasion.id,
            )
        )
    ).scalar_one_or_none()


def _clean_updates(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in _PLAN_FIELDS or value is None:
            continue
        if key in _LIST_FIELDS:
            if not isinstance(value, list):
                value = [value]
            value = [item for item in value if item not in (None, "")]
        elif key == "constraints":
            value = value if isinstance(value, dict) else {}
        elif isinstance(value, str):
            value = " ".join(value.strip().split())
            if not value:
                continue
        cleaned[key] = value
    return cleaned


async def upsert_occasion_gift_plan(
    db: AsyncSession,
    phone: str,
    occasion_reference: str | None,
    updates: dict[str, Any],
    *,
    create_if_missing: bool = False,
) -> OccasionGiftPlan | None:
    if not occasion_reference:
        return None
    authorized = await _authorized_user_and_occasion(db, phone, occasion_reference)
    if not authorized:
        return None
    user, occasion = authorized
    cleaned = _clean_updates(updates)
    if not cleaned:
        return None

    plan = (
        await db.execute(
            select(OccasionGiftPlan).where(
                OccasionGiftPlan.user_id == user.id,
                OccasionGiftPlan.occasion_id == occasion.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        if not create_if_missing:
            return None
        plan = OccasionGiftPlan(
            id=generate_id("gpl"),
            user_id=user.id,
            occasion_id=occasion.id,
            contact_id=occasion.contact_id,
        )
        db.add(plan)

    for key, value in cleaned.items():
        setattr(plan, key, value)
    await db.flush()
    return plan


async def persist_active_profile_to_plan(
    db: AsyncSession,
    phone: str,
    profile: GiftNeedProfile | None,
) -> OccasionGiftPlan | None:
    if not profile or not profile.occasion_id:
        return None
    has_planning_signal = any((
        profile.budget_min is not None,
        profile.budget_max is not None,
        profile.recipient_vibe,
        profile.interest,
        profile.gift_category,
        profile.selected_candidate_id,
        profile.selected_provider_id,
        profile.selected_brand,
    ))
    if not has_planning_signal:
        return None
    updates: dict[str, Any] = {
        "budget_min": profile.budget_min,
        "budget_max": profile.budget_max,
        "recipient_city": profile.recipient_city,
        "selected_candidate_id": profile.selected_candidate_id,
        "selected_provider_id": profile.selected_provider_id,
        "selected_brand": profile.selected_brand,
        "current_step": profile.current_step,
    }
    interests = [
        value
        for value in (
            profile.recipient_vibe,
            profile.interest,
            profile.gift_category,
        )
        if value
    ]
    if interests:
        updates["interests"] = list(dict.fromkeys(interests))
    return await upsert_occasion_gift_plan(
        db,
        phone,
        profile.occasion_id,
        updates,
        create_if_missing=True,
    )


async def profile_for_occasion(
    db: AsyncSession,
    phone: str,
    occasion_reference: str | None,
    *,
    base: GiftNeedProfile | None = None,
    plan: OccasionGiftPlan | None = None,
) -> GiftNeedProfile | None:
    """Build one authorized recipient profile, optionally enriched by its plan."""
    if not occasion_reference:
        return base
    authorized = await _authorized_user_and_occasion(
        db,
        phone,
        occasion_reference,
    )
    if not authorized:
        return None
    user, occasion = authorized
    if plan and (
        plan.occasion_id != occasion.id
        or plan.user_id != user.id
    ):
        plan = None

    profile = base or GiftNeedProfile()
    contact = (
        await db.get(Contact, occasion.contact_id)
        if occasion.contact_id
        else None
    )
    occasion_owner = (
        await db.get(User, occasion.user_id)
        if not contact and occasion.user_id != user.id
        else None
    )

    profile.occasion_id = profile.occasion_id or occasion.id
    profile.occasion_type = profile.occasion_type or (
        occasion.custom_label
        or occasion.type.value.replace("_", " ").title()
    )
    profile.occasion_date = profile.occasion_date or occasion.date.date().isoformat()
    profile.purpose = profile.purpose or (
        "self"
        if occasion.user_id == user.id and not occasion.contact_id
        else "gift_for_friend"
    )
    if contact:
        profile.recipient_name = profile.recipient_name or contact.name
        profile.recipient_phone = profile.recipient_phone or contact.phone
        profile.relationship = profile.relationship or contact.relationship_type
        profile.recipient_city = (
            profile.recipient_city
            or (plan.recipient_city if plan else None)
            or contact.city_id
            or contact.city_raw
        )
    elif occasion_owner:
        profile.recipient_name = profile.recipient_name or occasion_owner.name
        profile.recipient_phone = profile.recipient_phone or occasion_owner.phone
        profile.recipient_city = (
            profile.recipient_city
            or (plan.recipient_city if plan else None)
            or occasion_owner.city_id
            or occasion_owner.city_raw
        )

    if not plan:
        return profile
    profile.budget_min = (
        profile.budget_min if profile.budget_min is not None else plan.budget_min
    )
    profile.budget_max = (
        profile.budget_max if profile.budget_max is not None else plan.budget_max
    )
    if plan.interests:
        interest = ", ".join(str(value) for value in plan.interests if value)
        profile.interest = profile.interest or interest
        profile.recipient_vibe = profile.recipient_vibe or interest
    profile.selected_candidate_id = (
        profile.selected_candidate_id or plan.selected_candidate_id
    )
    profile.selected_provider_id = (
        profile.selected_provider_id or plan.selected_provider_id
    )
    profile.selected_brand = profile.selected_brand or plan.selected_brand
    profile.current_step = profile.current_step or plan.current_step
    return profile
