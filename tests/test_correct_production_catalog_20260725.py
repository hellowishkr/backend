"""Safety and completeness tests for the 2026-07-25 catalogue correction."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.catalog_taxonomy import GiftingOccasion, ProductCategory
from app.models.product import Product
from app.models.vendor import Vendor
from scripts.correct_production_catalog_20260725 import (
    CANONICAL_OCCASION_IDS,
    CATEGORY_UPDATES,
    DESCRIPTION_UPDATES,
    FieldUpdate,
    OCCASION_UPDATES,
    PRODUCT_NAMES,
    TARGET_PRODUCT_IDS,
    _normalise_occasions,
    correct_catalog,
    transition_needed,
)


EXISTING_CATEGORIES = (
    ("flowers", "Flowers"),
    ("cakes", "Cakes"),
    ("chocolates", "Chocolates"),
    ("candles", "Candles"),
    ("jewellery", "Jewellery"),
    ("hampers", "Hampers"),
    ("personalized", "Personalized"),
    ("experiences", "Experiences"),
)


@pytest.fixture
async def correction_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = (
        Vendor.__table__,
        Product.__table__,
        ProductCategory.__table__,
        GiftingOccasion.__table__,
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as db:
        db.add(
            Vendor(
                id="ven_catalog_correction_test",
                name="Catalogue Correction Test Vendor",
                category="test",
                fulfillment_mode="manual",
                active=True,
            )
        )
        for sort_order, (category_id, name) in enumerate(EXISTING_CATEGORIES):
            db.add(
                ProductCategory(
                    id=category_id,
                    name=name,
                    sort_order=sort_order,
                )
            )
        for sort_order, occasion_id in enumerate(sorted(CANONICAL_OCCASION_IDS)):
            db.add(
                GiftingOccasion(
                    id=occasion_id,
                    name=occasion_id.replace("-", " ").title(),
                    sort_order=sort_order,
                )
            )

        for product_id in sorted(TARGET_PRODUCT_IDS):
            category_update = CATEGORY_UPDATES.get(product_id)
            occasion_update = OCCASION_UPDATES.get(product_id)
            description_update = DESCRIPTION_UPDATES.get(product_id)
            db.add(
                Product(
                    id=product_id,
                    vendor_id="ven_catalog_correction_test",
                    name=PRODUCT_NAMES[product_id],
                    category=(category_update.expected if category_update else "Jewellery"),
                    occasions=list(occasion_update.expected if occasion_update else ("birthday",)),
                    description=(
                        description_update.expected
                        if description_update
                        else "An existing, product-specific description that is not targeted."
                    ),
                    base_price_paise=10_000,
                    active=True,
                    in_stock=True,
                    lifecycle_status="active",
                )
            )
        await db.commit()

    yield Session
    await engine.dispose()


def test_static_correction_manifest_is_complete_and_fact_safe():
    assert len(CATEGORY_UPDATES) == 12
    assert len(OCCASION_UPDATES) == 75
    assert len(DESCRIPTION_UPDATES) == 59
    assert len(TARGET_PRODUCT_IDS) == 84
    assert set(PRODUCT_NAMES) == TARGET_PRODUCT_IDS

    desired_occasion_ids = {
        occasion for update in OCCASION_UPDATES.values() for occasion in update.desired
    }
    assert desired_occasion_ids <= CANONICAL_OCCASION_IDS

    descriptions = [str(update.desired) for update in DESCRIPTION_UPDATES.values()]
    assert min(map(len, descriptions)) >= 160
    assert len(set(descriptions)) == len(descriptions)
    assert all(update.expected != update.desired for update in DESCRIPTION_UPDATES.values())


def test_transition_guard_accepts_audited_or_final_state_and_rejects_drift():
    update = FieldUpdate("before", "after")
    assert transition_needed("before", update, label="field") is True
    assert transition_needed("after", update, label="field") is False
    with pytest.raises(RuntimeError, match="drifted"):
        transition_needed("unexpected", update, label="field")

    occasion_update = FieldUpdate(("birthday",), ("birthday", "anniversary"))
    assert (
        transition_needed(
            ["birthday"],
            occasion_update,
            label="occasions",
            normalise=_normalise_occasions,
        )
        is True
    )
    assert (
        transition_needed(
            ["anniversary", "birthday"],
            occasion_update,
            label="occasions",
            normalise=_normalise_occasions,
        )
        is False
    )


@pytest.mark.asyncio
async def test_dry_run_commit_and_rerun_are_safe(correction_session_factory):
    dry_run = await correct_catalog(
        commit=False,
        session_factory=correction_session_factory,
        invalidate_cache=False,
    )
    assert dry_run.categories_created == 2
    assert dry_run.product_categories_changed == 12
    assert dry_run.occasions_changed == 75
    assert dry_run.descriptions_changed == 59

    async with correction_session_factory() as db:
        assert await db.get(ProductCategory, "home-fragrance") is None
        spray = await db.get(Product, "prd_gv_s_breeze")
        assert spray is not None
        assert spray.category == "Candles"
        assert spray.occasions == ["birthday", "diwali", "just-because"]

    committed = await correct_catalog(
        commit=True,
        session_factory=correction_session_factory,
        invalidate_cache=False,
    )
    assert committed == dry_run

    async with correction_session_factory() as db:
        categories = {
            category.id: category
            for category in (
                await db.execute(
                    select(ProductCategory).where(
                        ProductCategory.id.in_(("home-fragrance", "home-decor-keepsakes"))
                    )
                )
            )
            .scalars()
            .all()
        }
        assert categories["home-fragrance"].name == "Home Fragrance"
        assert categories["home-decor-keepsakes"].name == "Home Décor & Keepsakes"

        products = {
            product.id: product
            for product in (
                await db.execute(select(Product).where(Product.id.in_(TARGET_PRODUCT_IDS)))
            )
            .scalars()
            .all()
        }
        for product_id, update in CATEGORY_UPDATES.items():
            assert products[product_id].category == update.desired
        for product_id, update in OCCASION_UPDATES.items():
            assert products[product_id].occasions == list(update.desired)
        for product_id, update in DESCRIPTION_UPDATES.items():
            assert products[product_id].description == update.desired

    rerun = await correct_catalog(
        commit=True,
        session_factory=correction_session_factory,
        invalidate_cache=False,
    )
    assert rerun.total_changes == 0


@pytest.mark.asyncio
async def test_production_drift_aborts_before_other_changes(correction_session_factory):
    async with correction_session_factory() as db:
        spray = await db.get(Product, "prd_gv_s_breeze")
        assert spray is not None
        spray.category = "Unexpected Manual Category"
        await db.commit()

    with pytest.raises(RuntimeError, match="production drift"):
        await correct_catalog(
            commit=True,
            session_factory=correction_session_factory,
            invalidate_cache=False,
        )

    async with correction_session_factory() as db:
        assert await db.get(ProductCategory, "home-fragrance") is None
        untouched = await db.get(Product, "prd_gv_s_laven")
        assert untouched is not None
        assert untouched.category == "Candles"
