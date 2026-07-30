from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.api.admin_catalog import ProductPayload, VariantPayload, apply_product_payload
from app.api.catalog import check_pincode
from app.db.base import Base
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor


@pytest.fixture
async def catalog_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
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


def _payload(product_images: list[str], delivery_pincodes: list[str]) -> ProductPayload:
    return ProductPayload(
        vendor_id="ven_test",
        name="Gallery Gift Box",
        description="A gift with multiple gallery images.",
        category="gift-boxes",
        occasions=["birthday"],
        preparation_time="Ships tomorrow",
        dispatch_lead_time_days=1,
        delivery_pincodes=delivery_pincodes,
        is_bestseller=True,
        is_customisable=False,
        product_images=product_images,
        care_instructions="Keep dry.",
        whats_included="Gift box",
        customization_schema=[],
        base_price_paise=300000,
        discounted_price_paise=250000,
        inventory_quantity=7,
        gst_rate=Decimal("18.00"),
        hsn="1234",
        image_url="https://cdn.example.com/cover.jpg",
        active=True,
        variants=[],
        addons=[],
    )


async def _create_vendor(db: AsyncSession) -> None:
    db.add(
        Vendor(
            id="ven_test",
            name="Test Vendor",
            category="gifts",
            active=True,
        )
    )


@pytest.mark.asyncio
async def test_admin_product_save_round_trips_gallery_images(catalog_session_factory):
    initial_images = [
        "https://cdn.example.com/gallery-1.jpg",
        "https://cdn.example.com/gallery-2.jpg",
    ]

    async with catalog_session_factory() as db:
        await _create_vendor(db)
        product = Product(id="prd_test", vendor_id="ven_test", variants=[], addons=[])
        db.add(product)
        await apply_product_payload(db, product, _payload(initial_images, ["560001"]))
        await db.commit()

    async with catalog_session_factory() as db:
        product = (
            await db.execute(
                select(Product)
                .where(Product.id == "prd_test")
                .options(selectinload(Product.variants), selectinload(Product.addons))
            )
        ).scalar_one()
        updated_images = [
            "https://cdn.example.com/gallery-3.jpg",
            "https://cdn.example.com/gallery-4.jpg",
        ]
        await apply_product_payload(db, product, _payload(updated_images, ["560001"]))
        await db.commit()

    async with catalog_session_factory() as db:
        reloaded = await db.get(Product, "prd_test")

    assert reloaded is not None
    assert reloaded.product_images == updated_images
    assert reloaded.base_price_paise == 300000
    assert reloaded.discounted_price_paise == 250000
    assert reloaded.inventory_quantity == 7


@pytest.mark.asyncio
async def test_admin_product_save_round_trips_bouquet_recipe(catalog_session_factory):
    payload = _payload([], [])
    payload.is_customisable = True
    payload.variants = [
        VariantPayload(
            name="20 roses",
            price_delta_paise=49_900,
            seasonal_price_paise=79_900,
            seasonal_price_start=date(2026, 2, 7),
            seasonal_price_end=date(2026, 2, 14),
            bouquet_recipe={
                "enabled": True,
                "components": [
                    {
                        "name": "Red rose",
                        "kind": "flower",
                        "quantity": 20,
                        "unit_label": "stem",
                        "lot_size": 20,
                        "off_season_lot_cost_paise": 20_000,
                        "peak_season_lot_cost_paise": 60_000,
                    }
                ],
                "making_cost_paise": 10_000,
                "packaging_cost_paise": 9_000,
                "other_cost_paise": 0,
            },
            active=True,
            sort_order=1,
        )
    ]

    async with catalog_session_factory() as db:
        await _create_vendor(db)
        product = Product(id="prd_bouquet", vendor_id="ven_test", variants=[], addons=[])
        db.add(product)
        await apply_product_payload(db, product, payload)
        await db.commit()

    async with catalog_session_factory() as db:
        variant = (
            await db.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == "prd_bouquet"
                )
            )
        ).scalar_one()

    assert variant.name == "20 roses"
    assert variant.seasonal_price_paise == 79_900
    assert variant.seasonal_price_start == date(2026, 2, 7)
    assert variant.bouquet_recipe["components"][0]["lot_size"] == 20
    assert variant.bouquet_recipe["making_cost_paise"] == 10_000


@pytest.mark.asyncio
async def test_structured_pincode_check_uses_delivery_pincodes(catalog_session_factory):
    async with catalog_session_factory() as db:
        await _create_vendor(db)
        product = Product(id="prd_test", vendor_id="ven_test", variants=[], addons=[])
        db.add(product)
        await apply_product_payload(
            db,
            product,
            _payload(["https://cdn.example.com/gallery-1.jpg"], ["560001", "560002"]),
        )
        await db.commit()

    async with catalog_session_factory() as db:
        serviceable = await check_pincode("prd_test", "560001", db)
        unserviceable = await check_pincode("prd_test", "110001", db)

    assert serviceable == {"serviceable": True, "pincode": "560001"}
    assert unserviceable == {"serviceable": False, "pincode": "110001"}


def _preview_token(product_id: str, secret: str, expiry: int) -> str:
    import base64
    import hmac

    payload = f"{product_id}|{expiry}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload_b64.encode(), "sha256").hexdigest()
    return f"{payload_b64}.{sig}"


@pytest.mark.asyncio
async def test_pincode_check_inactive_product_requires_preview_token(
    catalog_session_factory, monkeypatch
):
    import time

    from app.core.config import settings
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "preview_signing_secret", "test-secret")

    async with catalog_session_factory() as db:
        await _create_vendor(db)
        product = Product(id="prd_test", vendor_id="ven_test", variants=[], addons=[])
        db.add(product)
        payload = _payload(["https://cdn.example.com/gallery-1.jpg"], ["560001"])
        payload.active = False
        await apply_product_payload(db, product, payload)
        await db.commit()

    async with catalog_session_factory() as db:
        with pytest.raises(HTTPException) as exc_info:
            await check_pincode("prd_test", "560001", db)
        assert exc_info.value.status_code == 404

    token = _preview_token("prd_test", "test-secret", int(time.time()) + 3600)
    async with catalog_session_factory() as db:
        serviceable = await check_pincode("prd_test", "560001", db, token)

    assert serviceable == {"serviceable": True, "pincode": "560001"}
