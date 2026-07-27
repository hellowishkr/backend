from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.sources import GiftCandidate
from app.bot.state_utils import get_state, update_active_gift_context
from app.bot.tools.tool_impl.gifting import save_gift_plan, suggest_gifts
from app.bot.tools.tool_impl.occasions import add_occasion, get_occasion_details
from app.core.config import settings
from app.models import (
    Contact,
    Occasion,
    OccasionGiftPlan,
    OccasionType,
    RecipientPreference,
    Reminder,
    ReminderStatus,
    User,
    WishlistItem,
)
from app.services.occasion_gift_plan import get_occasion_gift_plan


async def _seed_swaraj_wedding(db: AsyncSession, phone: str = "919000000701"):
    user = User(id="usr_plan", phone=phone, name="Disha")
    contact = Contact(
        id="con_plan",
        user_id=user.id,
        name="Swaraj",
        relationship_type="friend",
    )
    occasion = Occasion(
        id="occ_plan",
        user_id=user.id,
        contact_id=contact.id,
        type=OccasionType.WEDDING,
        date=datetime(2027, 7, 27, tzinfo=timezone.utc),
        recurring=False,
    )
    db.add_all([user, contact, occasion])
    await db.flush()
    return user, contact, occasion


@pytest.mark.asyncio
async def test_active_context_persists_private_per_occasion_plan(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)

    await update_active_gift_context(
        db,
        "919000000701",
        {
            "recipient_name": "Swaraj",
            "occasion_id": occasion.id,
            "occasion_type": "Wedding",
            "purpose": "gift_for_friend",
            "interest": "gaming",
            "recipient_vibe": "gaming",
            "budget_max": 3000,
            "current_step": "interest_known",
        },
    )

    plan = (
        await db.execute(select(OccasionGiftPlan))
    ).scalar_one()
    assert plan.occasion_id == occasion.id
    assert plan.budget_max == 3000
    assert plan.interests == ["gaming"]


@pytest.mark.asyncio
async def test_suggest_gifts_reuses_durable_budget_and_interest(
    db: AsyncSession,
    monkeypatch,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    await update_active_gift_context(
        db,
        "919000000701",
        {
            "recipient_name": "Swaraj",
            "occasion_id": occasion.id,
            "occasion_type": "Wedding",
            "purpose": "gift_for_friend",
            "interest": "gaming",
            "recipient_vibe": "gaming",
            "budget_max": 3000,
        },
    )
    # Simulate loss/expiry of transient state; the durable plan remains.
    from app.models import ConversationState
    state = (
        await db.execute(
            select(ConversationState).where(
                ConversationState.phone == "919000000701"
            )
        )
    ).scalar_one()
    await db.delete(state)
    await db.flush()

    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    with patch("app.bot.tools.tool_impl.gifting.SOURCES", []):
        result = await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id},
        )

    assert result["success"] is True
    assert result["known_fields"]["budget_max"] == 3000
    assert result["missing_fields"] == []
    assert result["clarifying_question"] is None
    assert result["mode"] == "ai_fallback_pending"
    assert result["suggestions"] == []
    assert all(
        item["availability"] == "general_idea"
        for item in result["suggestions"]
    )


@pytest.mark.asyncio
async def test_suggest_gifts_upserts_durable_plan_once(
    db: AsyncSession,
    monkeypatch,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)

    from app.services import occasion_gift_plan as plan_service

    tracked_upsert = AsyncMock(wraps=plan_service.upsert_occasion_gift_plan)
    with (
        patch("app.bot.tools.tool_impl.gifting.SOURCES", []),
        patch.object(
            plan_service,
            "upsert_occasion_gift_plan",
            tracked_upsert,
        ),
    ):
        result = await suggest_gifts(
            db,
            "919000000701",
            {
                "occasion_id": occasion.id,
                "budget_max": 3000,
                "vibes": ["mobile_gaming"],
                "recipient_vibe": "gaming",
            },
        )

    assert result["success"] is True
    assert tracked_upsert.await_count == 1
    plan = await get_occasion_gift_plan(
        db,
        "919000000701",
        occasion.id,
    )
    assert plan is not None
    assert plan.budget_max == 3000
    assert plan.current_step == "suggesting_gifts"
    assert plan.last_suggestions == []


@pytest.mark.asyncio
async def test_failed_optional_plan_sync_keeps_session_usable(
    db: AsyncSession,
) -> None:
    user, contact, occasion = await _seed_swaraj_wedding(db)
    await update_active_gift_context(
        db,
        user.phone,
        {
            "occasion_id": occasion.id,
            "purpose": "gift_for_friend",
            "budget_max": 3000,
        },
    )

    async def insert_duplicate_plan(
        session: AsyncSession,
        _phone: str,
        _profile,
    ) -> None:
        session.add(
            OccasionGiftPlan(
                id="gpl_duplicate",
                user_id=user.id,
                occasion_id=occasion.id,
                contact_id=contact.id,
            )
        )
        await session.flush()

    with patch(
        "app.services.occasion_gift_plan.persist_active_profile_to_plan",
        side_effect=insert_duplicate_plan,
    ):
        await update_active_gift_context(
            db,
            user.phone,
            {"current_step": "category_selected"},
        )

    # A failed insert was rolled back to the savepoint, not the outer
    # transaction; subsequent reads and writes remain valid.
    assert await db.scalar(select(func.count(OccasionGiftPlan.id))) == 1
    await update_active_gift_context(
        db,
        user.phone,
        {"gift_category": "headset"},
        sync_gift_plan=False,
    )


@pytest.mark.asyncio
async def test_gift_plan_is_not_visible_to_an_unrelated_user(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    await update_active_gift_context(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "purpose": "gift_for_friend",
            "budget_max": 3000,
        },
    )
    db.add(User(id="usr_other_plan", phone="919000000702", name="Other"))
    await db.flush()

    assert (
        await get_occasion_gift_plan(
            db,
            "919000000702",
            occasion.id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_saving_wedding_uses_one_time_default_without_creating_plan(
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(User(id="usr_wedding", phone="919000000703", name="Disha"))
    await db.flush()
    monkeypatch.setattr(
        "app.bot.tools.tool_impl.occasions.resolve_city_id",
        AsyncMock(
            return_value=SimpleNamespace(city_id=None, city_raw="Pune")
        ),
    )

    result = await add_occasion(
        db,
        "919000000703",
        {
            "contact_name": "Swaraj",
            "occasion_type": "WEDDING",
            "date_day": 27,
            "date_month": 7,
            "date_year": 2027,
            "recipient_city": "Pune",
        },
    )

    occasion = await db.get(Occasion, result["occasion_id"])
    assert result["recurring"] is False
    assert occasion.recurring is False
    contact = await db.get(Contact, occasion.contact_id)
    assert contact is not None
    assert contact.city_id or contact.city_raw
    assert (
        await db.scalar(
            select(OccasionGiftPlan).where(
                OccasionGiftPlan.occasion_id == occasion.id
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_explicit_occasion_switch_discards_previous_recipient_context(
    db: AsyncSession,
    monkeypatch,
) -> None:
    user, _, first = await _seed_swaraj_wedding(db)
    second_contact = Contact(
        id="con_plan_two",
        user_id=user.id,
        name="Vidushi",
        relationship_type="friend",
    )
    second = Occasion(
        id="occ_plan_two",
        user_id=user.id,
        contact_id=second_contact.id,
        type=OccasionType.BIRTHDAY,
        date=datetime(2027, 8, 2, tzinfo=timezone.utc),
        recurring=True,
    )
    db.add_all([second_contact, second])
    await db.flush()
    await update_active_gift_context(
        db,
        user.phone,
        {
            "recipient_name": "Swaraj",
            "occasion_id": first.id,
            "occasion_type": "Wedding",
            "purpose": "gift_for_friend",
            "recipient_vibe": "gaming",
            "interest": "gaming",
            "budget_max": 3000,
        },
    )

    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    with patch("app.bot.tools.tool_impl.gifting.SOURCES", []):
        result = await suggest_gifts(
            db,
            user.phone,
            {
                "occasion_id": second.id,
                "recipient_vibe": "books",
            },
        )

    assert result["known_fields"]["recipient_name"] == "Vidushi"
    assert "budget_max" not in result["known_fields"]
    active = (await get_state(db, user.phone))["active_gift_context"]
    assert active["occasion_id"] == second.id
    assert active["recipient_name"] == "Vidushi"
    assert active.get("budget_max") is None
    assert "gaming" not in active.get("interest", "")


@pytest.mark.asyncio
async def test_repeated_clarifying_question_is_suppressed(
    db: AsyncSession,
    monkeypatch,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)

    with patch("app.bot.tools.tool_impl.gifting.SOURCES", []):
        first = await suggest_gifts(db, "919000000701", {"occasion_id": occasion.id})
        second = await suggest_gifts(db, "919000000701", {"occasion_id": occasion.id})

    assert first["clarifying_question"] == "What budget would you like to stay within?"
    assert second["clarifying_question"] is None
    assert second["repeated_question_suppressed"] is True
    assert second["suggestions"] == []


@pytest.mark.asyncio
async def test_save_gift_plan_exposes_all_private_planning_fields(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)

    result = await save_gift_plan(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "budget_max": 3000,
            "interests": ["gaming"],
            "excluded_categories": ["clothing"],
            "preferred_brands": ["Logitech"],
            "gift_format": "physical",
            "constraints": {"platform": "PC"},
        },
    )

    assert result["success"] is True
    assert result["gift_plan"]["excluded_categories"] == ["clothing"]
    assert result["gift_plan"]["preferred_brands"] == ["Logitech"]
    assert result["gift_plan"]["constraints"] == {"platform": "PC"}


@pytest.mark.asyncio
async def test_saved_plan_preferences_filter_and_rank_recommendations(
    db: AsyncSession,
    monkeypatch,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    await save_gift_plan(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "budget_max": 3000,
            "interests": ["clothing"],
            "desired_categories": ["gaming"],
            "excluded_categories": ["clothing"],
            "preferred_brands": ["Logitech"],
            "disliked_brands": ["Razer"],
            "gift_format": "physical",
            "constraints": {"platform": "PC"},
        },
    )

    class _PlanAwareSource:
        seen_vibes: list[str] = []

        async def list_candidates(
            self,
            *,
            db,
            vibes,
            budget_min,
            budget_max,
            city,
        ):
            self.seen_vibes = list(vibes)
            return [
                GiftCandidate(
                    source_id="stub",
                    item_id="logitech_pc",
                    name="Logitech PC Controller",
                    vibes=["mobile_gaming"],
                    price_min=2000,
                    price_max=2500,
                    fulfillment="physical_delivery",
                    metadata={"category": "gaming"},
                ),
                GiftCandidate(
                    source_id="stub",
                    item_id="generic_console",
                    name="Generic Console Controller",
                    vibes=["gaming_console"],
                    price_min=1800,
                    price_max=2200,
                    fulfillment="physical_delivery",
                    metadata={"category": "gaming"},
                ),
                GiftCandidate(
                    source_id="stub",
                    item_id="razer_pc",
                    name="Razer PC Controller",
                    vibes=["mobile_gaming"],
                    price_min=2000,
                    price_max=2500,
                    fulfillment="physical_delivery",
                    metadata={"category": "gaming"},
                ),
                GiftCandidate(
                    source_id="stub",
                    item_id="hoodie",
                    name="Fashion Hoodie",
                    vibes=["gen_z_streetwear"],
                    price_min=1200,
                    price_max=1800,
                    fulfillment="physical_delivery",
                    metadata={"category": "clothing"},
                ),
                GiftCandidate(
                    source_id="stub",
                    item_id="logitech_card",
                    name="Logitech Gift Card",
                    vibes=["mobile_gaming"],
                    price_min=500,
                    price_max=3000,
                    fulfillment="instant_voucher",
                    metadata={"category": "gaming"},
                ),
            ]

    source = _PlanAwareSource()
    monkeypatch.setattr(settings, "mvp_reminder_only", False)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    with patch("app.bot.tools.tool_impl.gifting.SOURCES", [source]):
        result = await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id},
        )

    names = [item["name"] for item in result["suggestions"]]
    assert names == [
        "Logitech PC Controller",
        "Generic Console Controller",
    ]
    assert "mobile_gaming" in source.seen_vibes
    assert "gaming_console" in source.seen_vibes


@pytest.mark.asyncio
async def test_new_occasion_replaces_active_context_without_mutating_old_plan(
    db: AsyncSession,
    monkeypatch,
) -> None:
    user, _, first = await _seed_swaraj_wedding(db)
    monkeypatch.setattr(
        "app.bot.tools.tool_impl.occasions.resolve_city_id",
        AsyncMock(
            return_value=SimpleNamespace(city_id=None, city_raw="Delhi")
        ),
    )
    await update_active_gift_context(
        db,
        user.phone,
        {
            "recipient_name": "Swaraj",
            "occasion_id": first.id,
            "occasion_type": "Wedding",
            "purpose": "gift_for_friend",
            "budget_max": 3000,
            "interest": "gaming",
            "current_step": "interest_known",
        },
    )
    old_plan = await get_occasion_gift_plan(db, user.phone, first.id)
    assert old_plan is not None
    old_step = old_plan.current_step

    created = await add_occasion(
        db,
        user.phone,
        {
            "contact_name": "Vidushi",
            "occasion_type": "BIRTHDAY",
            "date_day": 2,
            "date_month": 8,
            "date_year": 2027,
            "recipient_city": "Delhi",
        },
    )

    active = (await get_state(db, user.phone))["active_gift_context"]
    refreshed_old_plan = await get_occasion_gift_plan(db, user.phone, first.id)
    new_plan = await get_occasion_gift_plan(
        db,
        user.phone,
        created["occasion_id"],
    )
    assert active["occasion_id"] == created["occasion_id"]
    assert active["recipient_name"] == "Vidushi"
    assert active.get("budget_max") is None
    assert active.get("interest") is None
    assert refreshed_old_plan is not None
    assert refreshed_old_plan.current_step == old_step
    assert refreshed_old_plan.budget_max == 3000
    assert new_plan is None


@pytest.mark.asyncio
async def test_new_occasion_clears_unscoped_active_planning_context(
    db: AsyncSession,
) -> None:
    phone = "919000000792"
    db.add(User(id="usr_unscoped_add", phone=phone, name="Disha"))
    await db.flush()
    await update_active_gift_context(
        db,
        phone,
        {
            "recipient_name": "Previous recipient",
            "purpose": "gift_for_friend",
            "budget_max": 3000,
            "interest": "gaming",
            "selected_candidate_id": "old_candidate",
        },
        sync_gift_plan=False,
    )

    created = await add_occasion(
        db,
        phone,
        {
            "contact_name": "Vidushi",
            "occasion_type": "BIRTHDAY",
            "date_day": 2,
            "date_month": 8,
            "date_year": 2027,
        },
    )

    active = (await get_state(db, phone))["active_gift_context"]
    assert active["occasion_id"] == created["occasion_id"]
    assert active["recipient_name"] == "Vidushi"
    assert active.get("budget_max") is None
    assert active.get("interest") is None
    assert active.get("selected_candidate_id") is None


@pytest.mark.asyncio
async def test_save_gift_plan_clears_unscoped_active_planning_context(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    await update_active_gift_context(
        db,
        "919000000701",
        {
            "recipient_name": "Previous recipient",
            "purpose": "gift_for_friend",
            "interest": "art",
            "selected_candidate_id": "old_candidate",
        },
        sync_gift_plan=False,
    )

    result = await save_gift_plan(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "budget_max": 3000,
        },
    )

    active = (await get_state(db, "919000000701"))["active_gift_context"]
    assert result["success"] is True
    assert active["occasion_id"] == occasion.id
    assert active["recipient_name"] == "Swaraj"
    assert active["budget_max"] == 3000
    assert active.get("interest") is None
    assert active.get("selected_candidate_id") is None


@pytest.mark.asyncio
async def test_progressive_budget_update_validates_against_saved_other_bound(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    first = await save_gift_plan(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "budget_min": 5000,
        },
    )
    second = await save_gift_plan(
        db,
        "919000000701",
        {
            "occasion_id": occasion.id,
            "budget_max": 3000,
        },
    )

    plan = await get_occasion_gift_plan(
        db,
        "919000000701",
        occasion.id,
    )
    assert first["success"] is True
    assert second["error"] == "budget_min cannot exceed budget_max"
    assert plan is not None
    assert plan.budget_min == 5000
    assert plan.budget_max is None


@pytest.mark.asyncio
async def test_ai_refinement_question_uses_same_repeat_guard(
    db: AsyncSession,
) -> None:
    responses = [
        {
            "success": True,
            "suggestions": [{"name": "Controller"}],
            "missing_fields": ["platform"],
            "clarifying_question": None,
            "recommendation_review": {
                "clarifying_question": "Does Swaraj play on PC or console?",
            },
        },
        {
            "success": True,
            "suggestions": [{"name": "Controller"}],
            "missing_fields": ["platform"],
            "clarifying_question": None,
            "recommendation_review": {
                "clarifying_question": "Does Swaraj play on PC or console?",
            },
        },
    ]
    with patch(
        "app.bot.tools.tool_impl.gifting.recommend_gifts",
        new=AsyncMock(side_effect=responses),
    ):
        first = await suggest_gifts(db, "919000000790", {"vibes": ["mobile_gaming"]})
        second = await suggest_gifts(db, "919000000790", {"vibes": ["mobile_gaming"]})

    assert (
        first["recommendation_review"]["clarifying_question"]
        == "Does Swaraj play on PC or console?"
    )
    assert second["recommendation_review"]["clarifying_question"] is None
    assert second["repeated_question_suppressed"] is True


@pytest.mark.asyncio
async def test_durable_last_suggestions_seed_exclusions_after_transient_loss(
    db: AsyncSession,
    monkeypatch,
) -> None:
    _, contact, occasion = await _seed_swaraj_wedding(db)
    db.add(
        OccasionGiftPlan(
            id="gpl_durable_names",
            user_id="usr_plan",
            occasion_id=occasion.id,
            contact_id=contact.id,
            vibe_ids=["mobile_gaming"],
            last_suggestions=[{"name": "Saved Headset"}],
        )
    )
    await db.flush()

    class _DurableSource:
        async def list_candidates(self, **kwargs):
            return [
                GiftCandidate(
                    source_id="stub",
                    item_id="saved",
                    name="Saved Headset",
                    vibes=["mobile_gaming"],
                    price_min=1000,
                    price_max=2000,
                    fulfillment="physical_delivery",
                ),
                GiftCandidate(
                    source_id="stub",
                    item_id="fresh",
                    name="Fresh Controller",
                    vibes=["mobile_gaming"],
                    price_min=1000,
                    price_max=2000,
                    fulfillment="physical_delivery",
                ),
            ]

    monkeypatch.setattr(settings, "mvp_reminder_only", False)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    with patch("app.bot.tools.tool_impl.gifting.SOURCES", [_DurableSource()]):
        result = await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id},
        )

    assert [item["name"] for item in result["suggestions"]] == [
        "Fresh Controller"
    ]


@pytest.mark.asyncio
async def test_explicit_occasion_suggestion_clears_unscoped_active_context(
    db: AsyncSession,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    await update_active_gift_context(
        db,
        "919000000701",
        {
            "recipient_name": "Previous recipient",
            "purpose": "gift_for_friend",
            "interest": "art",
            "budget_max": 5000,
            "selected_candidate_id": "old_candidate",
        },
        sync_gift_plan=False,
    )
    captured_requests = []

    async def _recommend(_db, request, *, sources):
        captured_requests.append(request)
        return {
            "success": True,
            "suggestions": [{"name": "Fresh wedding idea"}],
            "missing_fields": ["budget", "recipient_interests"],
            "clarifying_question": "What budget would you like to stay within?",
        }

    with patch(
        "app.bot.tools.tool_impl.gifting.recommend_gifts",
        side_effect=_recommend,
    ):
        await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id},
        )

    request = captured_requests[0]
    assert request.profile.recipient_name == "Swaraj"
    assert request.profile.interest is None
    assert request.profile.selected_candidate_id is None
    assert request.budget_max is None
    plan = await get_occasion_gift_plan(
        db,
        "919000000701",
        occasion.id,
    )
    assert plan is not None
    assert plan.budget_max is None
    assert plan.interests in (None, [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_key", "nested_name"),
    [
        ("catalogue_options", "Wishkr Gaming Mouse"),
        ("additional_general_ideas", "Personalised Controller Stand"),
    ],
)
async def test_nested_refinement_suggestions_are_saved_and_excluded_on_next_turn(
    db: AsyncSession,
    review_key: str,
    nested_name: str,
) -> None:
    _, _, occasion = await _seed_swaraj_wedding(db)
    captured_exclusions: list[list[str]] = []

    async def _recommend(_db, request, *, sources):
        captured_exclusions.append(list(request.exclude_names or []))
        return {
            "success": True,
            "suggestions": [{"name": "Mobile gaming controller"}],
            "missing_fields": [],
            "clarifying_question": None,
            "recommendation_review": {
                review_key: [{"name": nested_name}],
            },
        }

    with patch(
        "app.bot.tools.tool_impl.gifting.recommend_gifts",
        side_effect=_recommend,
    ):
        await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id, "vibes": ["mobile_gaming"]},
        )
        await suggest_gifts(
            db,
            "919000000701",
            {"occasion_id": occasion.id, "vibes": ["mobile_gaming"]},
        )

    assert nested_name in captured_exclusions[1]
    plan = await get_occasion_gift_plan(
        db,
        "919000000701",
        occasion.id,
    )
    assert plan is not None
    assert {
        item["name"]
        for item in plan.last_suggestions
    } == {"Mobile gaming controller", nested_name}


@pytest.mark.asyncio
async def test_idempotent_occasion_retry_preserves_explicit_recurrence(
    db: AsyncSession,
) -> None:
    phone = "919000000791"
    db.add(User(id="usr_recurrence_retry", phone=phone, name="Disha"))
    await db.flush()

    recurring_wedding = await add_occasion(
        db,
        phone,
        {
            "contact_name": "Swaraj",
            "occasion_type": "WEDDING",
            "date_day": 20,
            "date_month": 12,
            "date_year": 2027,
            "recurring": True,
        },
    )
    wedding_retry = await add_occasion(
        db,
        phone,
        {
            "contact_name": "Swaraj",
            "occasion_type": "WEDDING",
            "date_day": 20,
            "date_month": 12,
        },
    )
    one_time_anniversary = await add_occasion(
        db,
        phone,
        {
            "contact_name": "Asha",
            "occasion_type": "ANNIVERSARY",
            "date_day": 21,
            "date_month": 12,
            "date_year": 2027,
            "recurring": False,
        },
    )
    anniversary_retry = await add_occasion(
        db,
        phone,
        {
            "contact_name": "Asha",
            "occasion_type": "ANNIVERSARY",
            "date_day": 21,
            "date_month": 12,
        },
    )

    wedding = await db.get(Occasion, recurring_wedding["occasion_id"])
    anniversary = await db.get(
        Occasion,
        one_time_anniversary["occasion_id"],
    )
    assert wedding_retry["reused"] is True
    assert anniversary_retry["reused"] is True
    assert wedding is not None and wedding.recurring is True
    assert anniversary is not None and anniversary.recurring is False


@pytest.mark.asyncio
async def test_occasion_details_include_wishlists_shortlist_and_reminders(
    db: AsyncSession,
) -> None:
    user, contact, occasion = await _seed_swaraj_wedding(db)
    contact.phone = "919000000799"
    recipient = User(
        id="usr_plan_recipient",
        phone=contact.phone,
        name="Swaraj",
    )
    db.add_all([
        recipient,
        RecipientPreference(
            id="rpf_plan",
            recipient_phone=contact.phone,
            wishlist_links='["https://example.com/external"]',
            source="direct_response",
        ),
        WishlistItem(
            id="wsh_plan_recipient",
            user_id=recipient.id,
            url="https://example.com/native",
            purpose="self",
            visibility="public",
        ),
        WishlistItem(
            id="wsh_plan_giver",
            user_id=user.id,
            url="https://example.com/shortlist",
            purpose="gift",
            recipient_occasion_id=occasion.id,
        ),
        Reminder(
            id="rem_plan",
            occasion_id=occasion.id,
            scheduled_at=datetime(2027, 7, 26, tzinfo=timezone.utc),
            status=ReminderStatus.PENDING,
            message_type="one_day_before_reminder",
        ),
    ])
    await db.flush()

    details = await get_occasion_details(
        db,
        user.phone,
        {"occasion_id": occasion.id},
    )

    assert details["whose"] == "Swaraj"
    assert details["contact_id"] == contact.id
    assert "contact" not in details
    assert details["recipient_preferences_available"] is True
    assert details["recipient_wishlist_count"] == 2
    assert details["giver_shortlist_count"] == 1
    assert details["reminder_schedule"]["pending_count"] == 1
    assert details["reminder_schedule"]["sent_count"] == 0
    assert details["reminder_schedule"]["total_count"] == 1
    assert "reminders" not in details["reminder_schedule"]
