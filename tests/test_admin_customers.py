from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_customers import get_customer, list_customers
from app.db.base import Base
from app.models.order import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.user import User
from app.models.vendor import Vendor


@pytest.fixture
async def customer_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Vendor.__table__,
                Order.__table__,
                OrderLineItem.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    db.add(User(id="usr_1", phone="9999999999", name="Asha Rao", email="asha@example.com"))
    db.add(Vendor(id="ven_1", name="Petal & Co", category="flowers", active=True))
    db.add(
        Order(
            id="ord_1",
            user_id="usr_1",
            total_amount=Decimal("1500.00"),
            status=OrderStatus.PAID,
            order_type="product",
            vendor_id="ven_1",
            shipping_name="Asha Rao",
            shipping_phone="9999999999",
            shipping_line1="221B Baker Street",
            shipping_city="Pune",
            shipping_state="MH",
            shipping_pincode="411001",
            delivery_date=date(2026, 1, 1),
        )
    )
    db.add(
        Order(
            id="ord_2",
            user_id="usr_1",
            total_amount=Decimal("500.00"),
            status=OrderStatus.COMPLETED,
            order_type="product",
            vendor_id="ven_1",
            shipping_name="Asha Rao",
            shipping_phone="9999999999",
            shipping_line1="221B Baker Street",
            shipping_city="Pune",
            shipping_state="MH",
            shipping_pincode="411001",
        )
    )
    await db.commit()


async def test_list_customers_aggregates_order_count_and_spend(customer_session_factory):
    async with customer_session_factory() as db:
        await _seed(db)
        result = await list_customers(db=db, q=None, page=1, limit=100)

    assert len(result["customers"]) == 1
    customer = result["customers"][0]
    assert customer["id"] == "usr_1"
    assert customer["order_count"] == 2
    assert customer["total_spend"] == 2000.0


async def test_list_customers_filters_by_query(customer_session_factory):
    async with customer_session_factory() as db:
        await _seed(db)
        result = await list_customers(db=db, q="asha", page=1, limit=100)
        assert len(result["customers"]) == 1

        result = await list_customers(db=db, q="nonexistent", page=1, limit=100)
        assert len(result["customers"]) == 0


async def test_get_customer_returns_deduped_addresses_and_orders(customer_session_factory):
    async with customer_session_factory() as db:
        await _seed(db)
        result = await get_customer(user_id="usr_1", db=db)

    assert result["customer"]["order_count"] == 2
    assert result["customer"]["total_spend"] == 2000.0
    assert len(result["addresses"]) == 1
    assert result["addresses"][0]["pincode"] == "411001"
    assert len(result["orders"]) == 2
    assert result["orders"][0]["vendor_name"] == "Petal & Co"
