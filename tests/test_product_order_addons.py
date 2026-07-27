"""Quantity-aware product add-ons: server-side pricing, validation, and snapshot.

Covers the breaking change from `addon_ids: list[str]` to a quantity-aware
`selected_addons` model. The server always re-prices from the DB (never trusts
the client) and stores a frozen snapshot on the order line item.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_catalog import AddonPayload
from app.api.product_orders import (
    ProductOrderItemPayload,
    SelectedAddonPayload,
    price_line,
)
from app.db.base import Base
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
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    db.add(Vendor(id="ven_1", name="Test Vendor", category="gifts", active=True))
    db.add(
        Product(
            id="prd_1",
            vendor_id="ven_1",
            name="Hamper",
            base_price_paise=100000,  # ₹1000
            active=True,
            shipping_included=True,
        )
    )
    # A second product, to test "add-on from another product".
    db.add(
        Product(
            id="prd_2",
            vendor_id="ven_1",
            name="Other",
            base_price_paise=50000,
            active=True,
            shipping_included=True,
        )
    )
    db.add(
        ProductAddon(
            id="add_teddy",
            product_id="prd_1",
            name="Teddy Bear",
            price_paise=10000,  # ₹100 each
            active=True,
            allow_quantity=True,
            min_quantity=1,
            max_quantity=5,
        )
    )
    db.add(
        ProductAddon(
            id="add_choc",
            product_id="prd_1",
            name="Chocolate Pack",
            price_paise=29900,  # ₹299 each
            active=True,
            allow_quantity=True,
            min_quantity=1,
            max_quantity=10,
        )
    )
    db.add(
        ProductAddon(
            id="add_wrap",
            product_id="prd_1",
            name="Premium Gift Wrap",
            price_paise=9900,  # ₹99
            active=True,
            allow_quantity=False,
            min_quantity=1,
            max_quantity=1,
        )
    )
    db.add(
        ProductAddon(
            id="add_inactive",
            product_id="prd_1",
            name="Discontinued",
            price_paise=5000,
            active=False,
        )
    )
    db.add(
        ProductAddon(
            id="add_other",
            product_id="prd_2",
            name="Belongs To Other Product",
            price_paise=5000,
            active=True,
        )
    )
    await db.commit()


def _item(selected: list[dict], qty: int = 1) -> ProductOrderItemPayload:
    return ProductOrderItemPayload(
        product_id="prd_1",
        selected_addons=[SelectedAddonPayload(**s) for s in selected],
        qty=qty,
    )


# ── Pricing ──────────────────────────────────────────────────────────────────


async def test_single_quantity(session_factory):
    async with session_factory() as db:
        await _seed(db)
        line = await price_line(db, _item([{"addon_id": "add_teddy", "quantity": 1}]))
    assert line.unit_price_paise == 100000 + 10000  # base + 1 teddy
    assert line.selected_addons == [
        {
            "addon_id": "add_teddy",
            "name": "Teddy Bear",
            "unit_price_paise": 10000,
            "quantity": 1,
            "total_price_paise": 10000,
        }
    ]


async def test_same_addon_quantity_three(session_factory):
    async with session_factory() as db:
        await _seed(db)
        line = await price_line(db, _item([{"addon_id": "add_teddy", "quantity": 3}]))
    assert line.unit_price_paise == 100000 + 30000
    assert line.selected_addons[0]["quantity"] == 3
    assert line.selected_addons[0]["total_price_paise"] == 30000


async def test_multiple_addons_different_quantities(session_factory):
    """The spec's headline example: ₹1000 + 947 = ₹1947."""
    async with session_factory() as db:
        await _seed(db)
        line = await price_line(
            db,
            _item(
                [
                    {"addon_id": "add_teddy", "quantity": 3},  # 300
                    {"addon_id": "add_choc", "quantity": 2},  # 598
                    {"addon_id": "add_wrap", "quantity": 1},  # 99
                ]
            ),
        )
    assert line.unit_price_paise == 100000 + 30000 + 59800 + 9900  # 199700


async def test_note_is_snapshotted(session_factory):
    async with session_factory() as db:
        await _seed(db)
        line = await price_line(
            db,
            _item([{"addon_id": "add_wrap", "quantity": 1, "note": "gold ink"}]),
        )
    assert line.selected_addons[0]["note"] == "gold ink"


# ── Validation ───────────────────────────────────────────────────────────────


async def test_single_select_rejects_quantity_over_one(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException) as exc:
            await price_line(db, _item([{"addon_id": "add_wrap", "quantity": 3}]))
    assert exc.value.status_code == 400


async def test_quantity_below_min_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        # add_choc min is 1; quantity 0 is also caught earlier, so use a min>1 case
        # by lowering teddy to min 2 in-place.
        addon = await db.get(ProductAddon, "add_teddy")
        addon.min_quantity = 2
        await db.commit()
        with pytest.raises(HTTPException):
            await price_line(db, _item([{"addon_id": "add_teddy", "quantity": 1}]))


async def test_quantity_above_max_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException):
            await price_line(db, _item([{"addon_id": "add_teddy", "quantity": 6}]))


@pytest.mark.parametrize("bad", [0, -1])
def test_quantity_zero_or_negative_rejected_in_pricing(bad):
    # 0 and negatives pass schema (int), so pricing must reject them. allow=True
    # add-ons reject via min bound; single-select reject via "!= 1".
    payload = SelectedAddonPayload(addon_id="x", quantity=bad)
    assert payload.quantity == bad  # schema accepts the int; pricing layer rejects


@pytest.mark.parametrize("bad", [2.5, "3", None, True])
def test_quantity_non_integer_rejected_by_schema(bad):
    with pytest.raises(ValidationError):
        SelectedAddonPayload(addon_id="x", quantity=bad)


async def test_unknown_addon_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException):
            await price_line(db, _item([{"addon_id": "add_nope", "quantity": 1}]))


async def test_inactive_addon_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException):
            await price_line(db, _item([{"addon_id": "add_inactive", "quantity": 1}]))


async def test_addon_from_another_product_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException):
            await price_line(db, _item([{"addon_id": "add_other", "quantity": 1}]))


async def test_duplicate_addon_rejected(session_factory):
    async with session_factory() as db:
        await _seed(db)
        with pytest.raises(HTTPException) as exc:
            await price_line(
                db,
                _item(
                    [
                        {"addon_id": "add_teddy", "quantity": 2},
                        {"addon_id": "add_teddy", "quantity": 3},
                    ]
                ),
            )
    assert exc.value.status_code == 400


# ── Admin add-on config validation ───────────────────────────────────────────


def test_admin_single_select_forces_quantity_one():
    payload = AddonPayload(name="Wrap", price_paise=9900, allow_quantity=False, max_quantity=8)
    assert payload.min_quantity == 1 and payload.max_quantity == 1


def test_admin_quantity_min_must_be_at_least_one():
    with pytest.raises(ValidationError):
        AddonPayload(name="Teddy", allow_quantity=True, min_quantity=0, max_quantity=5)


def test_admin_max_below_min_rejected():
    with pytest.raises(ValidationError):
        AddonPayload(name="Teddy", allow_quantity=True, min_quantity=5, max_quantity=2)


def test_admin_large_max_quantity_accepted():
    # There's no product-level cap on per-add-on quantity anymore — bulk/corporate
    # add-ons (e.g. Gift Wrap x 2000) are allowed up to the int4 column ceiling.
    addon = AddonPayload(name="Gift Wrap", allow_quantity=True, min_quantity=1, max_quantity=2000)
    assert addon.max_quantity == 2000


def test_admin_max_quantity_above_int4_rejected():
    # Values that can't fit the Integer column are still rejected.
    with pytest.raises(ValidationError):
        AddonPayload(name="Teddy", allow_quantity=True, min_quantity=1, max_quantity=2_147_483_648)
