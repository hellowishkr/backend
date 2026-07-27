"""Admin product delete: removes an unordered product, but refuses one that has
order history (order_line_items.product_id is ondelete=RESTRICT).
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_catalog import delete_product
from app.db.base import Base
from app.models.order_line_item import OrderLineItem
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
                OrderLineItem.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed_product(db: AsyncSession, product_id: str) -> None:
    db.add(Vendor(id="ven_1", name="V", category="gifts", active=True))
    db.add(
        Product(
            id=product_id,
            vendor_id="ven_1",
            name="Dupe",
            base_price_paise=0,
            variants=[],
            addons=[],
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_delete_unordered_product(session_factory):
    async with session_factory() as db:
        await _seed_product(db, "prd_free")
        res = await delete_product("prd_free", db)
        assert res == {"ok": True}
        count = (
            await db.execute(select(func.count()).select_from(Product))
        ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_delete_ordered_product_is_blocked(session_factory):
    async with session_factory() as db:
        await _seed_product(db, "prd_ordered")
        db.add(
            OrderLineItem(
                id="oli_1",
                order_id="ord_1",
                vendor_id="ven_1",
                product_id="prd_ordered",
                product_name="Ordered Product",
                qty=1,
                unit_price_paise=100,
                line_total_paise=100,
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await delete_product("prd_ordered", db)
        assert exc.value.status_code == 409

        # Product must survive the refusal.
        assert await db.get(Product, "prd_ordered") is not None


@pytest.mark.asyncio
async def test_delete_missing_product_404(session_factory):
    async with session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await delete_product("prd_nope", db)
        assert exc.value.status_code == 404
