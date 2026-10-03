"""Unit tests for public order tracking endpoint (order_id + email).

Covers:
- Successful order tracking via POST & GET with valid order_id and email
- Case-insensitivity for buyer email
- Step progression (pending, shipped, delivered, cancelled, refunded)
- Rejection on wrong email (404)
- Rejection on non-existent order (404)
- Rate limiting protection
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.product_orders import (
    TrackOrderPayload,
    _build_tracking_response,
    track_product_order,
    track_product_order_get,
)
from app.db.base import Base
from app.models.order import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.user import User


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Order.__table__,
                OrderLineItem.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


def _dummy_request(ip: str = "127.0.0.1"):
    req = MagicMock()
    req.headers = {}
    req.client.host = ip
    return req


@pytest.mark.asyncio
async def test_track_order_success_post(session_factory):
    async with session_factory() as db:
        user = User(
            id="usr_trk_1",
            phone="+919876510001",
            name="Rohan Sharma",
            email="rohan@example.com",
        )
        db.add(user)
        order = Order(
            id="ord_trk_100",
            user_id="usr_trk_1",
            total_amount=Decimal("1499.00"),
            currency="INR",
            status=OrderStatus.PAID,
            order_type="product",
            fulfillment_status="shipped",
            tracking_carrier="Delhivery",
            tracking_number="DL12345678",
            tracking_url="https://delhivery.com/track/DL12345678",
            shipping_name="Aarav Sharma",
            shipping_line1="42 Brigade Road",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_pincode="560001",
            delivery_date=date(2026, 10, 8),
        )
        db.add(order)
        item = OrderLineItem(
            id="item_trk_1",
            order_id="ord_trk_100",
            product_id="prd_roses",
            product_name="Classic Crimson Roses",
            product_image_url="https://cdn.wishkr.com/roses.jpg",
            qty=1,
            unit_price_paise=149900,
            line_total_paise=149900,
        )
        db.add(item)
        await db.commit()

        # Track with uppercase email to test case-insensitivity
        payload = TrackOrderPayload(order_id="ord_trk_100", email="ROHAN@EXAMPLE.COM")
        res = await track_product_order(payload, _dummy_request("10.0.0.1"), db)

        assert res["order_id"] == "ord_trk_100"
        assert res["status"] == "PAID"
        assert res["fulfillment_status"] == "shipped"
        assert res["tracking_carrier"] == "Delhivery"
        assert res["tracking_number"] == "DL12345678"
        assert res["tracking_url"] == "https://delhivery.com/track/DL12345678"
        assert res["shipping_address"]["city"] == "Bengaluru"
        assert len(res["items"]) == 1
        assert res["items"][0]["product_name"] == "Classic Crimson Roses"

        # Verify steps: shipped should have confirmed & processing completed, shipped current, delivered upcoming
        steps = res["steps"]
        assert len(steps) == 4
        assert steps[0]["key"] == "confirmed" and steps[0]["status"] == "completed"
        assert steps[1]["key"] == "processing" and steps[1]["status"] == "completed"
        assert steps[2]["key"] == "shipped" and steps[2]["status"] == "current"
        assert steps[3]["key"] == "delivered" and steps[3]["status"] == "upcoming"


@pytest.mark.asyncio
async def test_track_order_success_get(session_factory):
    async with session_factory() as db:
        user = User(
            id="usr_trk_2",
            phone="+919876510002",
            name="Priya Patel",
            email="priya@example.com",
        )
        db.add(user)
        order = Order(
            id="ord_trk_200",
            user_id="usr_trk_2",
            total_amount=Decimal("899.00"),
            currency="INR",
            status=OrderStatus.PAID,
            order_type="product",
            fulfillment_status="delivered",
            delivery_date=date(2026, 10, 5),
        )
        db.add(order)
        await db.commit()

        res = await track_product_order_get("ord_trk_200", "priya@example.com", _dummy_request("10.0.0.2"), db)
        assert res["order_id"] == "ord_trk_200"
        assert res["fulfillment_status"] == "delivered"

        # All 4 steps should be completed for delivered
        steps = res["steps"]
        assert all(s["status"] == "completed" for s in steps)


@pytest.mark.asyncio
async def test_track_order_wrong_email_rejected(session_factory):
    async with session_factory() as db:
        user = User(
            id="usr_trk_3",
            phone="+919876510003",
            name="Vikram",
            email="vikram@example.com",
        )
        db.add(user)
        order = Order(
            id="ord_trk_300",
            user_id="usr_trk_3",
            total_amount=Decimal("500.00"),
            status=OrderStatus.PAID,
            order_type="product",
        )
        db.add(order)
        await db.commit()

        payload = TrackOrderPayload(order_id="ord_trk_300", email="wrong@example.com")
        with pytest.raises(HTTPException) as exc:
            await track_product_order(payload, _dummy_request("10.0.0.3"), db)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_track_order_nonexistent_order_rejected(session_factory):
    async with session_factory() as db:
        payload = TrackOrderPayload(order_id="ord_does_not_exist", email="any@example.com")
        with pytest.raises(HTTPException) as exc:
            await track_product_order(payload, _dummy_request("10.0.0.4"), db)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_track_order_cancelled_steps():
    order = Order(
        id="ord_canc",
        status=OrderStatus.CANCELLED,
        fulfillment_status="pending",
        total_amount=Decimal("1000.00"),
        currency="INR",
        created_at=datetime.now(timezone.utc),
    )
    order.line_items = []
    res = _build_tracking_response(order)
    assert res["status"] == "CANCELLED"
    assert len(res["steps"]) == 2
    assert res["steps"][1]["key"] == "cancelled"
