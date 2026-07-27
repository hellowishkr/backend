"""Tests for the optional context-aware gift recommendation review."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_context import GiftNeedProfile
from app.core.config import settings
from app.models import (
    ConversationMessage,
    LlmUsageEvent,
    MessageRole,
    Product,
    User,
    Vendor,
)
from app.services.gift_refinement import (
    GiftRefinementRequest,
    _build_context,
    _catalogue_products,
    refine_gift_recommendations,
)
from app.services.storefront_pricing import StorefrontPricing


def _suggestions() -> list[dict]:
    return [
        {
            "name": "Soft everyday hoodie",
            "why": "comfortable everyday option",
            "budget_hint": "around ₹1,200-₹2,500",
        },
        {
            "name": "Sketchbook and brush-pen set",
            "why": "works across several art styles",
            "budget_hint": "around ₹500-₹1,200",
        },
    ]


@pytest.mark.asyncio
async def test_refinement_uses_redacted_last_ten_messages_and_preserves_candidates(
    db: AsyncSession,
    monkeypatch,
) -> None:
    phone = "919876543210"
    db.add(User(id="usr_refine", phone=phone, name="Disha"))
    for index in range(12):
        content = f"message {index}"
        if index == 11:
            content = "Call 9876543210 or email disha@example.com https://example.com"
        db.add(ConversationMessage(
            phone=phone,
            role=MessageRole.USER if index % 2 == 0 else MessageRole.MODEL,
            content=content,
        ))
    db.add(ConversationMessage(
        phone=phone,
        role=MessageRole.TOOL_RESPONSE,
        content="private tool response",
    ))
    await db.flush()

    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "mvp_reminder_only", True)

    model_output = json.dumps({
        "summary": "The hoodie is the safer main gift, with art supplies as an add-on.",
        "recommended_indexes": [1, 2, 999],
        "reasons": {
            "1": "It directly matches her request for comfy clothes.",
            "2": "It supports her art interest without assuming one medium.",
        },
        "additional_general_ideas": [
            {
                "name": "Personalised art pouch",
                "why": "Useful for carrying pens and brushes.",
                "budget_hint": "around ₹400-₹900",
            },
        ],
        "catalogue_product_indexes": [],
        "clarifying_question": "What is her clothing size?",
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._call_deepseek",
            new=AsyncMock(return_value=(
                model_output,
                {
                    "input_tokens": 300,
                    "output_tokens": 90,
                    "total_tokens": 390,
                },
            )),
        ) as call_deepseek,
        patch(
            "app.services.gift_refinement._call_gemini",
            new=AsyncMock(),
        ) as call_gemini,
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone=phone,
                suggestions=_suggestions(),
                profile=GiftNeedProfile(
                    recipient_name="Vidushi",
                    relationship="friend",
                    recipient_vibe="comfy clothes, art supplies",
                ),
            ),
        )

    assert result is not None
    assert result["provider"] == "deepseek"
    assert result["context_messages_used"] == 10
    assert result["wishkr_catalogue_match_found"] is False
    assert [item["name"] for item in result["top_picks"]] == [
        "Soft everyday hoodie",
        "Sketchbook and brush-pen set",
    ]
    assert result["top_picks"][0]["budget_hint"] == "around ₹1,200-₹2,500"
    assert result["additional_general_ideas"][0]["availability"] == "general_idea"
    assert result["clarifying_question"] == "What is her clothing size?"

    payload = json.loads(call_deepseek.await_args.args[0])
    recent_text = " ".join(item["content"] for item in payload["recent_messages"])
    assert len(payload["recent_messages"]) == 10
    assert "private tool response" not in recent_text
    assert "9876543210" not in recent_text
    assert "disha@example.com" not in recent_text
    assert "https://example.com" not in recent_text
    assert "[phone removed]" in recent_text
    call_gemini.assert_not_awaited()

    usage_rows = (await db.execute(select(LlmUsageEvent))).scalars().all()
    assert len(usage_rows) == 1
    assert usage_rows[0].purpose == "gift_refinement"
    assert usage_rows[0].provider == "deepseek"
    assert usage_rows[0].success is True


@pytest.mark.asyncio
async def test_refinement_falls_back_to_gemini_after_deepseek_timeout(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")

    gemini_output = json.dumps({
        "summary": "The art set is the most personal option.",
        "recommended_indexes": [2],
        "reasons": {"2": "It directly fits the known interest."},
        "additional_general_ideas": [],
        "catalogue_product_indexes": [],
        "clarifying_question": None,
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._call_deepseek",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ),
        patch(
            "app.services.gift_refinement._call_gemini",
            new=AsyncMock(return_value=(gemini_output, {})),
        ),
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000501",
                suggestions=_suggestions(),
            ),
        )

    assert result is not None
    assert result["provider"] == "google_genai"
    assert result["top_picks"][0]["name"] == "Sketchbook and brush-pen set"

    usage_rows = (
        await db.execute(select(LlmUsageEvent).order_by(LlmUsageEvent.created_at))
    ).scalars().all()
    assert len(usage_rows) == 2
    assert usage_rows[0].provider == "deepseek"
    assert usage_rows[0].success is False
    assert usage_rows[0].error_type == "timeout"
    assert usage_rows[1].provider == "google_genai"
    assert usage_rows[1].success is True
    assert usage_rows[1].fallback_used is True


@pytest.mark.asyncio
async def test_refinement_returns_exact_catalogue_rows_selected_by_index(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_key", "")

    catalogue_rows = [{
        "product_id": "prd_canvas_kit",
        "name": "Wishkr Mini Canvas Kit",
        "brand": "Wishkr Partner",
        "category": "Art",
        "description": "Canvas and paints",
        "price_rupees": 899,
        "browse_url": "https://wishkr.com/shop/prd_canvas_kit",
    }]
    model_output = json.dumps({
        "summary": "The catalogue kit matches the known art interest.",
        "recommended_indexes": [1],
        "reasons": {"1": "A comfortable option."},
        "additional_general_ideas": [{
            "name": "Another art kit",
            "why": "A generated alternative.",
            "budget_hint": "around ₹500",
        }],
        "catalogue_product_indexes": [1, 99],
        "clarifying_question": None,
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._catalogue_products",
            new=AsyncMock(return_value=catalogue_rows),
        ),
        patch(
            "app.services.gift_refinement._call_gemini",
            new=AsyncMock(return_value=(model_output, {})),
        ),
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000502",
                suggestions=_suggestions(),
            ),
        )

    assert result is not None
    assert result["catalogue_options"] == catalogue_rows
    assert result["wishkr_catalogue_match_found"] is True
    assert result["additional_general_ideas"] == []
    assert "exact products" in result["catalogue_message"]


@pytest.mark.asyncio
async def test_refinement_skips_model_when_budget_is_blocked(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(False, "per_user_daily")),
        ),
        patch(
            "app.services.gift_refinement._call_gemini",
            new=AsyncMock(),
        ) as call_gemini,
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000503",
                suggestions=_suggestions(),
            ),
        )

    assert result is None
    call_gemini.assert_not_awaited()


@pytest.mark.asyncio
async def test_refinement_generates_when_deterministic_suggestions_are_empty(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-key")

    output = json.dumps({
        "summary": "Gaming ideas fit the known interest and budget.",
        "recommended_indexes": [],
        "reasons": {},
        "additional_general_ideas": [{
            "name": "Mobile gaming controller",
            "why": "It fits a mobile gamer and stays below ₹3,000.",
            "budget_hint": "around ₹1,000-₹2,500",
        }],
        "catalogue_product_indexes": [],
        # A provider must not be allowed to re-ask a known budget.
        "clarifying_question": "What budget do you have in mind?",
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._catalogue_products",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.gift_refinement._call_deepseek",
            new=AsyncMock(return_value=(output, {})),
        ) as deepseek,
        patch(
            "app.services.gift_refinement._call_gemini",
            new=AsyncMock(),
        ) as gemini,
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000504",
                suggestions=[],
                profile=GiftNeedProfile(
                    recipient_name="Swaraj",
                    occasion_type="Wedding",
                    interest="gaming",
                ),
                budget_max=3000,
            ),
        )

    assert result is not None
    assert result["provider"] == "deepseek"
    assert result["top_picks"] == []
    assert result["additional_general_ideas"][0]["name"] == "Mobile gaming controller"
    assert result["clarifying_question"] is None
    deepseek.assert_awaited_once()
    gemini.assert_not_awaited()


@pytest.mark.asyncio
async def test_refinement_validates_all_generated_idea_text_against_hard_constraints(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    output = json.dumps({
        "summary": "Generated alternatives.",
        "recommended_indexes": [],
        "reasons": {},
        "additional_general_ideas": [
            {
                "name": "Premium surprise box",
                "why": "A clothing bundle selected for the recipient.",
                "budget_hint": "around ₹2,000",
            },
            {
                "name": "Desk upgrade",
                "why": "A Razer accessory for their setup.",
                "budget_hint": "around ₹2,500",
            },
            {
                "name": "Gaming treat",
                "why": "A digital voucher for their favourite game.",
                "budget_hint": "around ₹1,500",
            },
            {
                "name": "Previously shown idea",
                "why": "A physical desk accessory.",
                "budget_hint": "around ₹1,000",
            },
        ],
        "catalogue_product_indexes": [],
        "clarifying_question": None,
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._catalogue_products",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.gift_refinement._call_deepseek",
            new=AsyncMock(return_value=(output, {})),
        ),
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000505",
                suggestions=[],
                excluded_categories=["clothing"],
                disliked_brands=["Razer"],
                gift_format="physical",
                exclude_names=["Previously shown idea"],
            ),
        )

    assert result is not None
    assert result["additional_general_ideas"] == []


@pytest.mark.asyncio
async def test_refinement_rejects_generated_ideas_outside_saved_budget(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "gift_refinement_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-key")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    output = json.dumps({
        "summary": "Budget-aware alternatives.",
        "recommended_indexes": [],
        "reasons": {},
        "additional_general_ideas": [
            {
                "name": "Premium gaming headset",
                "why": "A physical gaming accessory.",
                "budget_hint": "around ₹1,000-₹3,000",
            },
            {
                "name": "Gaming thumb sleeves",
                "why": "A small physical gaming accessory.",
                "budget_hint": "around ₹300-₹500",
            },
        ],
        "catalogue_product_indexes": [],
        "clarifying_question": None,
    })

    with (
        patch(
            "app.services.gift_refinement.check_all_budgets",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "app.services.gift_refinement._catalogue_products",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.gift_refinement._call_deepseek",
            new=AsyncMock(return_value=(output, {})),
        ),
    ):
        result = await refine_gift_recommendations(
            db,
            GiftRefinementRequest(
                phone="919000000508",
                suggestions=[],
                budget_max=500,
                gift_format="physical",
            ),
        )

    assert result is not None
    assert [
        idea["name"]
        for idea in result["additional_general_ideas"]
    ] == ["Gaming thumb sleeves"]


@pytest.mark.asyncio
async def test_catalogue_filters_before_limit_and_treats_desired_category_as_preference(
    db: AsyncSession,
) -> None:
    excluded_rows = [
        {
            "product_id": f"prd_excluded_{index}",
            "name": f"Clothing item {index}",
            "brand": "Other",
            "category": "Clothing",
            "description": "A shirt",
            "price_rupees": 1000,
            "browse_url": f"https://wishkr.com/shop/prd_excluded_{index}",
        }
        for index in range(20)
    ]
    valid_rows = [
        {
            "product_id": "prd_preferred_brand",
            "name": "Wireless accessory",
            "brand": "Logitech",
            "category": "Electronics",
            "description": "A useful desk accessory",
            "price_rupees": 2000,
            "browse_url": "https://wishkr.com/shop/prd_preferred_brand",
        },
        {
            "product_id": "prd_desired_category",
            "name": "Gaming controller",
            "brand": "Other",
            "category": "Gaming",
            "description": "A controller",
            "price_rupees": 1800,
            "browse_url": "https://wishkr.com/shop/prd_desired_category",
        },
    ]

    with patch(
        "app.services.gift_refinement._catalogue_products",
        new=AsyncMock(return_value=[*excluded_rows, *valid_rows]),
    ):
        context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000506",
                suggestions=[],
                desired_categories=["gaming"],
                excluded_categories=["clothing"],
                preferred_brands=["Logitech"],
            ),
        )

    names = [
        product["name"]
        for product in context["wishkr_catalogue_products"]
    ]
    assert names == ["Wireless accessory", "Gaming controller"]


@pytest.mark.asyncio
async def test_read_only_wishkr_catalogue_is_available_in_reminder_only_mode(
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Vendor(
        id="vnd_catalogue",
        name="Wishkr Partner",
        category="gifts",
        active=True,
    ))
    db.add(Product(
        id="prd_catalogue",
        vendor_id="vnd_catalogue",
        name="Gaming Desk Lamp",
        category="Gaming",
        base_price_paise=199900,
        active=True,
        in_stock=True,
        lifecycle_status="active",
    ))
    await db.flush()

    monkeypatch.setattr(settings, "mvp_reminder_only", True)
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=StorefrontPricing(
            markup_pct=Decimal("0"),
            flash_off_pct=Decimal("0"),
            flash_active=False,
        )),
    ):
        rows = await _catalogue_products(db)
        context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000507",
                suggestions=[],
                disliked_brands=["Wishkr Partner"],
            ),
        )

    assert rows[0]["product_id"] == "prd_catalogue"
    assert rows[0]["brand"] == "Wishkr Partner"
    assert rows[0]["browse_url"].endswith("/shop/prd_catalogue")
    assert context["wishkr_catalogue_products"] == []


@pytest.mark.asyncio
async def test_catalogue_uses_selling_price_inventory_and_vendor_format(
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Vendor(
        id="vnd_experience",
        name="Celebration Rooms",
        category="private_theatre",
        active=True,
    ))
    db.add_all([
        Product(
            id="prd_experience",
            vendor_id="vnd_experience",
            name="Celebration Package",
            category="Packages",
            base_price_paise=250000,
            discounted_price_paise=180000,
            inventory_quantity=2,
            active=True,
            in_stock=True,
            lifecycle_status="active",
        ),
        Product(
            id="prd_sold_out",
            vendor_id="vnd_experience",
            name="Sold-out Package",
            category="Packages",
            base_price_paise=150000,
            inventory_quantity=0,
            active=True,
            in_stock=True,
            lifecycle_status="active",
        ),
    ])
    await db.flush()
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=StorefrontPricing(
            markup_pct=Decimal("0"),
            flash_off_pct=Decimal("0"),
            flash_active=False,
        )),
    ):
        rows = await _catalogue_products(db)
        experience_context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000509",
                suggestions=[],
                budget_max=1800,
                gift_format="experience",
            ),
        )
        below_minimum_context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000509",
                suggestions=[],
                budget_min=2000,
                gift_format="experience",
            ),
        )
        physical_context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000509",
                suggestions=[],
                gift_format="physical",
            ),
        )

    assert [row["product_id"] for row in rows] == ["prd_experience"]
    assert rows[0]["price_rupees"] == 1800
    assert rows[0]["vendor_category"] == "private_theatre"
    assert [
        product["name"]
        for product in experience_context["wishkr_catalogue_products"]
    ] == ["Celebration Package"]
    assert below_minimum_context["wishkr_catalogue_products"] == []
    assert physical_context["wishkr_catalogue_products"] == []


@pytest.mark.asyncio
async def test_catalogue_uses_flash_checkout_price_for_budget_filtering(
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Vendor(
        id="vnd_flash",
        name="Flash Partner",
        category="gifts",
        active=True,
    ))
    db.add(Product(
        id="prd_flash",
        vendor_id="vnd_flash",
        name="Flash Gift",
        category="Gifts",
        base_price_paise=250000,
        discounted_price_paise=180000,
        inventory_quantity=2,
        active=True,
        in_stock=True,
        lifecycle_status="active",
    ))
    await db.flush()
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)
    flash_pricing = StorefrontPricing(
        markup_pct=Decimal("20"),
        flash_off_pct=Decimal("0"),
        flash_active=True,
    )

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=flash_pricing),
    ):
        rows = await _catalogue_products(db)
        over_budget = await _catalogue_products(
            db,
            GiftRefinementRequest(
                phone="919000000512",
                suggestions=[],
                budget_max=2000,
            ),
        )

    assert rows[0]["price_rupees"] == 3000
    assert over_budget == []


@pytest.mark.asyncio
async def test_catalogue_brand_preferences_only_match_vendor_brand(
    db: AsyncSession,
) -> None:
    rows = [
        {
            "product_id": "prd_compatible",
            "name": "PC Controller",
            "brand": "Other Brand",
            "vendor_category": "gifts",
            "category": "Gaming",
            "description": "Compatible with Razer devices",
            "price_rupees": 1800,
            "browse_url": "https://wishkr.com/shop/prd_compatible",
        },
        {
            "product_id": "prd_razer",
            "name": "PC Accessory",
            "brand": "Razer",
            "vendor_category": "gifts",
            "category": "Gaming",
            "description": "A useful controller accessory",
            "price_rupees": 2000,
            "browse_url": "https://wishkr.com/shop/prd_razer",
        },
    ]

    with patch(
        "app.services.gift_refinement._catalogue_products",
        new=AsyncMock(return_value=rows),
    ):
        disliked_context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000510",
                suggestions=[],
                disliked_brands=["Razer"],
            ),
        )
        preferred_context = await _build_context(
            db,
            GiftRefinementRequest(
                phone="919000000510",
                suggestions=[],
                preferred_brands=["Razer"],
            ),
        )

    assert [
        product["name"]
        for product in disliked_context["wishkr_catalogue_products"]
    ] == ["PC Controller"]
    assert preferred_context["wishkr_catalogue_products"][0]["name"] == "PC Accessory"


@pytest.mark.asyncio
async def test_catalogue_database_query_uses_one_bounded_ranking_pool(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)
    vendor = SimpleNamespace(name="Other Brand", category="gifts")

    def _product(index: int, *, category: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"prd_{index:03d}",
            name=f"Product {index}",
            category=category,
            description="",
            base_price_paise=100000,
            discounted_price_paise=None,
        )

    rows = [
        (_product(index, category="Clothing"), vendor)
        for index in range(100)
    ]
    fake_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(all=lambda: rows)
        )
    )

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=StorefrontPricing(
            markup_pct=Decimal("0"),
            flash_off_pct=Decimal("0"),
            flash_active=False,
        )),
    ):
        rows = await _catalogue_products(
            fake_db,
            GiftRefinementRequest(
                phone="919000000513",
                suggestions=[],
                excluded_categories=["clothing"],
            ),
        )

    assert rows == []
    fake_db.execute.assert_awaited_once()
    statement = fake_db.execute.await_args.args[0]
    assert statement._limit_clause is not None
    assert statement._limit_clause.value == 100


@pytest.mark.asyncio
async def test_catalogue_soft_preferences_rank_across_overfetched_pool(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)
    other_vendor = SimpleNamespace(name="Other Brand", category="gifts")
    preferred_vendor = SimpleNamespace(name="Razer", category="gifts")
    page = [
        (
            SimpleNamespace(
                id=f"prd_rank_{index:03d}",
                name=f"Product {index}",
                category="Gaming",
                description="",
                base_price_paise=100000,
                discounted_price_paise=None,
            ),
            preferred_vendor if index == 99 else other_vendor,
        )
        for index in range(100)
    ]
    fake_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(all=lambda: page)
        )
    )
    request = GiftRefinementRequest(
        phone="919000000514",
        suggestions=[],
        preferred_brands=["Razer"],
    )
    normal_pricing = StorefrontPricing(
        markup_pct=Decimal("0"),
        flash_off_pct=Decimal("0"),
        flash_active=False,
    )

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=normal_pricing),
    ):
        ranking_pool = await _catalogue_products(fake_db, request)
    with patch(
        "app.services.gift_refinement._catalogue_products",
        new=AsyncMock(return_value=ranking_pool),
    ):
        context = await _build_context(db, request)

    assert len(ranking_pool) == 100
    assert context["wishkr_catalogue_products"][0]["name"] == "Product 99"
    assert len(context["wishkr_catalogue_products"]) == 20


@pytest.mark.asyncio
async def test_impossible_catalogue_filter_stops_after_one_bounded_query(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)
    vendor = SimpleNamespace(name="Other Brand", category="gifts")
    rows = [
        (
            SimpleNamespace(
                id=f"prd_blocked_{index}",
                name=f"Blocked product {index}",
                category="Clothing",
                description="",
                base_price_paise=100000,
                discounted_price_paise=None,
            ),
            vendor,
        )
        for index in range(100)
    ]
    fake_db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(all=lambda: rows)
        )
    )

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=StorefrontPricing(
            markup_pct=Decimal("0"),
            flash_off_pct=Decimal("0"),
            flash_active=False,
        )),
    ):
        result = await _catalogue_products(
            fake_db,
            GiftRefinementRequest(
                phone="919000000515",
                suggestions=[],
                excluded_categories=["clothing"],
            ),
        )

    assert result == []
    fake_db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_price_sql_minimum_matches_whole_rupee_checkout_ceiling(
    db: AsyncSession,
    monkeypatch,
) -> None:
    db.add(Vendor(
        id="vnd_rounding",
        name="Rounding Partner",
        category="gifts",
        active=True,
    ))
    db.add(Product(
        id="prd_rounding",
        vendor_id="vnd_rounding",
        name="Rounding Gift",
        category="Gifts",
        base_price_paise=100001,
        discounted_price_paise=None,
        inventory_quantity=1,
        active=True,
        in_stock=True,
        lifecycle_status="active",
    ))
    await db.flush()
    monkeypatch.setattr(settings, "bot_product_catalog_enabled", True)

    with patch(
        "app.services.gift_refinement.get_storefront_pricing",
        new=AsyncMock(return_value=StorefrontPricing(
            markup_pct=Decimal("0"),
            flash_off_pct=Decimal("0"),
            flash_active=False,
        )),
    ):
        rows = await _catalogue_products(
            db,
            GiftRefinementRequest(
                phone="919000000516",
                suggestions=[],
                budget_min=1001,
            ),
        )

    assert [row["product_id"] for row in rows] == ["prd_rounding"]
    assert rows[0]["price_rupees"] == 1001
