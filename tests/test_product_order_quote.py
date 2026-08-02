"""Backend-authoritative marketplace checkout quote tests."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.product_orders import (
    ProductOrderItemPayload,
    allocate_whole_rupee_total,
    build_product_order_quote,
    enforce_allocation_minimums,
    serialize_product_order_quote,
)
from app.db.base import Base
from app.models.admin_user import PricingSetting
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Vendor.__table__,
                Product.__table__,
                ProductVariant.__table__,
                ProductAddon.__table__,
                PricingSetting.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(
    db: AsyncSession,
    *,
    markup_pct: Decimal = Decimal("0"),
    flash_off_pct: Decimal = Decimal("0"),
    flash_active: bool = False,
) -> None:
    db.add(
        PricingSetting(
            id=1,
            markup_pct=markup_pct,
            flash_off_pct=flash_off_pct,
            flash_active=flash_active,
        )
    )
    db.add(
        Vendor(
            id="ven_one",
            name="PyariSakhee",
            category="gifts",
            active=True,
            shipping_flat_fee_paise=5900,
            shipping_extra_item_fee_paise=3000,
            delivery_lead_time_hours=48,
        )
    )
    db.add(
        Product(
            id="prd_one",
            vendor_id="ven_one",
            name="Silver Earrings",
            base_price_paise=125000,
            discounted_price_paise=100000,
            inventory_quantity=100,
            active=True,
            shipping_included=False,
            shipping_fee_paise=7000,
            dispatch_lead_time_days=2,
            image_url="https://example.com/earrings.jpg",
        )
    )
    db.add(
        Product(
            id="prd_two",
            vendor_id="ven_one",
            name="Silver Bangle",
            base_price_paise=63700,
            inventory_quantity=None,
            active=True,
            shipping_included=True,
            dispatch_lead_time_days=4,
        )
    )
    await db.commit()


def _item(product_id: str, qty: int = 1) -> ProductOrderItemPayload:
    return ProductOrderItemPayload(product_id=product_id, qty=qty)


def test_list_allocation_rebalances_rounding_without_changing_total() -> None:
    assert enforce_allocation_minimums([100, 0], [0, 100]) == [0, 100]


def test_list_allocation_reports_an_upstream_pricing_invariant() -> None:
    with pytest.raises(RuntimeError, match="list total is below the charged total"):
        enforce_allocation_minimums([100, 0], [100, 100])


async def test_delivery_is_free_when_order_is_at_least_500_rupees(session_factory) -> None:
    async with session_factory() as db:
        await _seed(db)
        quote = await build_product_order_quote(db, [_item("prd_one", qty=100)])

    body = serialize_product_order_quote(quote)
    assert quote.shipping_paise == 0
    assert quote.total_paise == 10_000_000
    assert body["vendor"] == {"id": "ven_one", "name": "PyariSakhee"}
    assert body["shipping"]["charge_type"] == "free"
    assert body["shipping"]["fee_paise"] == 0
    assert body["shipping"]["free_delivery_threshold_paise"] == 50_000
    assert body["shipping"]["amount_until_free_delivery_paise"] == 0
    assert body["items"][0]["vendor_name"] == "PyariSakhee"
    assert body["items"][0]["earliest_delivery_date"]
    assert body["breakdown"]["total_paise"] == quote.total_paise
    assert body["breakdown"]["product_list_subtotal_paise"] == 12_500_000
    assert body["breakdown"]["discount_paise"] == 2_500_000


async def test_vendor_flat_shipping_applies_below_500_rupees(session_factory) -> None:
    async with session_factory() as db:
        await _seed(db)
        product = await db.get(Product, "prd_one")
        assert product is not None
        product.base_price_paise = 40_000
        product.discounted_price_paise = 40_000
        await db.commit()
        quote = await build_product_order_quote(db, [_item("prd_one")])

    body = serialize_product_order_quote(quote)
    assert quote.product_subtotal_paise == 40_000
    assert quote.shipping_paise == 5_900
    assert quote.total_paise == 45_900
    assert body["shipping"]["charge_type"] == "vendor_flat"
    assert body["shipping"]["amount_until_free_delivery_paise"] == 10_000


async def test_quote_line_breakdown_sums_exactly_to_backend_aggregate(session_factory) -> None:
    async with session_factory() as db:
        await _seed(
            db,
            markup_pct=Decimal("30"),
            flash_off_pct=Decimal("20"),
            flash_active=True,
        )
        quote = await build_product_order_quote(
            db,
            [_item("prd_one"), _item("prd_two")],
        )

    assert sum(quote.line_product_subtotals_paise) == quote.product_subtotal_paise
    assert sum(quote.line_list_subtotals_paise) == quote.product_list_subtotal_paise
    assert sum(quote.line_discounts_paise) == quote.discount_paise
    assert all(
        listed == charged + discount
        for listed, charged, discount in zip(
            quote.line_list_subtotals_paise,
            quote.line_product_subtotals_paise,
            quote.line_discounts_paise,
            strict=True,
        )
    )
    assert quote.earliest_delivery_date == max(quote.line_earliest_delivery_dates)


async def test_inactive_flash_uses_vendor_discount_and_base_as_list(session_factory) -> None:
    async with session_factory() as db:
        await _seed(
            db,
            markup_pct=Decimal("30"),
            flash_off_pct=Decimal("20"),
            flash_active=False,
        )
        quote = await build_product_order_quote(db, [_item("prd_one")])

    assert quote.base_product_total_paise == 125_000
    assert quote.raw_product_total_paise == 100_000
    assert quote.product_list_subtotal_paise == 125_000
    assert quote.product_subtotal_paise == 100_000
    assert quote.discount_paise == 25_000


async def test_active_flash_uses_base_and_ignores_normal_discount(session_factory) -> None:
    async with session_factory() as db:
        await _seed(
            db,
            markup_pct=Decimal("30"),
            flash_off_pct=Decimal("20"),
            flash_active=True,
        )
        quote = await build_product_order_quote(db, [_item("prd_one")])

    assert quote.base_product_total_paise == 125_000
    assert quote.raw_product_total_paise == 100_000
    assert quote.product_list_subtotal_paise == 162_500
    assert quote.product_subtotal_paise == 130_000
    assert quote.discount_paise == 32_500


async def test_quote_uses_active_seasonal_bouquet_variant_price(session_factory) -> None:
    async with session_factory() as db:
        await _seed(db)
        db.add(
            ProductVariant(
                id="var_40_roses",
                product_id="prd_one",
                name="40 roses",
                price_delta_paise=79_900,
                discounted_price_paise=69_900,
                seasonal_price_paise=119_900,
                seasonal_price_start=date(2020, 1, 1),
                seasonal_price_end=date(2030, 12, 31),
                active=True,
                sort_order=1,
            )
        )
        await db.commit()
        quote = await build_product_order_quote(
            db,
            [
                ProductOrderItemPayload(
                    product_id="prd_one",
                    variant_id="var_40_roses",
                    qty=1,
                )
            ],
        )

    assert quote.base_product_total_paise == 119_900
    assert quote.raw_product_total_paise == 119_900
    assert quote.product_list_subtotal_paise == 119_900
    assert quote.product_subtotal_paise == 119_900


async def test_quote_rejects_products_from_different_vendors(session_factory) -> None:
    async with session_factory() as db:
        await _seed(db)
        db.add(Vendor(id="ven_two", name="Other Seller", category="gifts", active=True))
        db.add(
            Product(
                id="prd_three",
                vendor_id="ven_two",
                name="Other Gift",
                base_price_paise=50000,
                active=True,
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await build_product_order_quote(
                db,
                [_item("prd_one"), _item("prd_three")],
            )

    assert exc.value.status_code == 400
    assert exc.value.detail == "product orders must contain one vendor"


async def test_quote_rejects_combined_lines_above_finite_inventory(session_factory) -> None:
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException) as exc:
            await build_product_order_quote(
                db,
                [_item("prd_one", qty=60), _item("prd_one", qty=41)],
            )

    assert exc.value.status_code == 409
    assert exc.value.detail == "Only 100 unit(s) of 'Silver Earrings' are currently available"


def test_quote_allocation_is_deterministic_and_exact() -> None:
    assert allocate_whole_rupee_total(10_000, [1, 1, 1]) == [3400, 3300, 3300]


def test_product_order_accepts_100_units_but_not_101() -> None:
    assert ProductOrderItemPayload(product_id="prd_one", qty=100).qty == 100
    with pytest.raises(ValidationError):
        ProductOrderItemPayload(product_id="prd_one", qty=101)
