"""Validation and idempotence tests for the EG2I catalogue sync."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor
from scripts.sync_eg2i_catalog_20260821 import (
    Catalogue,
    EXPECTED_PRODUCTS,
    load_catalogue,
    product_id_for_sku,
    sync_catalogue,
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Vendor.__table__,
                Product.__table__,
                ProductVariant.__table__,
                ProductAddon.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


def test_load_catalogue_has_all_verified_products() -> None:
    catalogue = load_catalogue()

    assert len(catalogue.products) == EXPECTED_PRODUCTS == 150
    assert len({product.sku.casefold() for product in catalogue.products}) == 150
    assert len({product_id_for_sku(product.sku) for product in catalogue.products}) == 150
    assert {product.category for product in catalogue.products} <= {
        "Flowers",
        "Cakes",
        "Chocolates",
        "Hampers",
    }
    assert all(product.images for product in catalogue.products)
    assert all(product.price_paise > 0 for product in catalogue.products)
    assert sum(product.seasonal for product in catalogue.products) == 124
    assert sum("rakhi" in product.occasions for product in catalogue.products) == 130


@pytest.mark.asyncio
async def test_sync_creates_published_products_and_is_idempotent(session_factory) -> None:
    source = load_catalogue()
    catalogue = Catalogue(vendor=source.vendor, products=source.products[:2])

    async with session_factory() as db:
        first = await sync_catalogue(db, catalogue, publish=True)
        await db.commit()

    assert first.vendor_created is True
    assert first.products_created == 2

    async with session_factory() as db:
        vendor = await db.get(Vendor, first.vendor_id)
        products = (
            await db.execute(select(Product).where(Product.vendor_id == first.vendor_id))
        ).scalars().all()

    assert vendor is not None
    assert vendor.name == "EG2I"
    assert vendor.active is True
    assert vendor.onboarding_status == "live"
    assert len(products) == 2
    assert all(product.active for product in products)
    assert all(product.lifecycle_status == "active" for product in products)
    assert all(product.in_stock for product in products)
    assert all(product.supports_gift_message for product in products)
    assert all(product.image_url for product in products)

    async with session_factory() as db:
        second = await sync_catalogue(db, catalogue, publish=True)
        await db.rollback()

    assert second.products_created == 0
    assert second.products_updated == 0
    assert second.products_unchanged == 2
    assert second.total_changes == 0


@pytest.mark.asyncio
async def test_sync_can_stage_products_as_drafts(session_factory) -> None:
    source = load_catalogue()
    catalogue = Catalogue(vendor=source.vendor, products=source.products[:1])

    async with session_factory() as db:
        summary = await sync_catalogue(db, catalogue, publish=False)
        await db.commit()

    async with session_factory() as db:
        product = await db.get(Product, product_id_for_sku(catalogue.products[0].sku))
        vendor = await db.get(Vendor, summary.vendor_id)

    assert product is not None
    assert product.active is False
    assert product.lifecycle_status == "draft"
    assert vendor is not None
    assert vendor.active is False
    assert vendor.onboarding_status == "kyc_done"


@pytest.mark.asyncio
async def test_sync_can_pause_and_restore_rakhi_products(session_factory) -> None:
    catalogue = load_catalogue()

    async with session_factory() as db:
        await sync_catalogue(
            db,
            catalogue,
            publish=True,
            disabled_occasions=frozenset({"rakhi"}),
        )
        await db.commit()

    async with session_factory() as db:
        products = (await db.execute(select(Product))).scalars().all()

    rakhi_products = [product for product in products if "rakhi" in product.occasions]
    other_products = [product for product in products if "rakhi" not in product.occasions]
    assert len(rakhi_products) == 130
    assert len(other_products) == 20
    assert all(not product.active for product in rakhi_products)
    assert all(product.lifecycle_status == "paused" for product in rakhi_products)
    assert all(product.active for product in other_products)

    async with session_factory() as db:
        await sync_catalogue(db, catalogue, publish=True)
        await db.commit()

    async with session_factory() as db:
        restored = (await db.execute(select(Product))).scalars().all()

    assert all(product.active for product in restored)
    assert all(product.lifecycle_status == "active" for product in restored)
