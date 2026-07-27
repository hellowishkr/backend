from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.sources import GiftCandidate
from app.core.config import settings
from app.services.gift_recommendation import (
    GiftRecommendationRequest,
    _idea_suggestions,
    recommend_gifts,
)
from app.utils.text_matching import contains_match_value, count_match_values


def test_shared_text_matcher_preserves_phrase_boundaries() -> None:
    assert contains_match_value("A wall art kit", "wall_art")
    assert not contains_match_value("Party supplies", "art")
    assert count_match_values("Razer gaming mouse", ["razer", "gaming"]) == 2


def test_non_cold_start_vibes_defer_to_ai_refinement() -> None:
    assert _idea_suggestions(["mobile_gaming"], 3) == []
    assert _idea_suggestions(["salon_spa_voucher"], 3) == []


def test_deterministic_ideas_respect_saved_budget_range() -> None:
    assert _idea_suggestions(
        ["gen_z_streetwear"],
        3,
        budget_max=500,
    ) == []

    boundary_match = _idea_suggestions(
        ["wall_art"],
        3,
        budget_min=500,
        budget_max=500,
    )
    assert [item["name"] for item in boundary_match] == [
        "Sketchbook and brush-pen set"
    ]


@pytest.mark.asyncio
async def test_non_cold_start_vibe_materializes_validated_ai_ideas(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    review = {
        "reviewed": True,
        "wishkr_catalogue_match_found": False,
        "additional_general_ideas": [
            {
                "name": "Mobile gaming controller",
                "why": "Fits their gaming interest.",
                "budget_hint": "around ₹1,000-₹2,500",
                "availability": "general_idea",
            }
        ],
    }

    with patch(
        "app.services.gift_refinement.refine_gift_recommendations",
        new=AsyncMock(return_value=review),
    ):
        result = await recommend_gifts(
            db,
            GiftRecommendationRequest(
                phone="919000000512",
                vibes_in=["mobile_gaming"],
                budget_max=3000,
            ),
            sources=[],
        )

    assert result["mode"] == "ai_general_ideas"
    assert result["suggestions"] == review["additional_general_ideas"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget_args",
    [
        {"budget_text": "under 500"},
        {"budget_max": "500"},
    ],
)
async def test_normalized_budgets_reach_candidate_and_fallback_filters(
    db: AsyncSession,
    monkeypatch,
    budget_args: dict,
) -> None:
    class _OverBudgetSource:
        source_id = "over_budget"
        seen_budget_max = None

        async def list_candidates(
            self,
            *,
            db,
            vibes,
            budget_min,
            budget_max,
            city,
        ):
            self.seen_budget_max = budget_max
            return [
                GiftCandidate(
                    source_id=self.source_id,
                    item_id="headset",
                    name="Premium gaming headset",
                    vibes=["mobile_gaming"],
                    price_min=1500,
                    price_max=3000,
                    fulfillment="physical_delivery",
                )
            ]

    source = _OverBudgetSource()
    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "gift_refinement_enabled", False)
    with patch(
        "app.services.gift_recommendation.enabled_sources",
        return_value=[],
    ):
        result = await recommend_gifts(
            db,
            GiftRecommendationRequest(
                phone="919000000511",
                vibes_in=["mobile_gaming"],
                **budget_args,
            ),
            sources=[source],
        )

    assert source.seen_budget_max == 500
    assert result["mode"] == "ai_fallback_pending"
    assert result["suggestions"] == []
