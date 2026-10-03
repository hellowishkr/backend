"""Unit tests for customer-facing order email notifications.

Covers:
- send_order_shipped_email
- send_order_delivered_email
- send_order_cancelled_email
- send_order_refunded_email
- update_fulfillment status transitions in admin_orders
- cancel_order endpoint in admin_orders
- resend_fulfillment_notification endpoint in admin_orders
"""

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_orders import (
    CancelOrderPayload,
    FulfillmentPayload,
    cancel_order,
    resend_fulfillment_notification,
    update_fulfillment,
)
from app.db.base import Base
from app.models.admin_user import AuditLog
from app.models.order import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.order_payment_link import OrderPaymentLink
from app.models.user import User
from app.services.order_notifications import (
    send_order_cancelled_email,
    send_order_delivered_email,
    send_order_refunded_email,
    send_order_shipped_email,
)


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
                OrderPaymentLink.__table__,
                AuditLog.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_send_order_shipped_email(session_factory):
    async with session_factory() as db:
        user = User(id="usr_shipped", name="Aarav Sharma", email="aarav@example.com")
        db.add(user)
        order = Order(
            id="ord_shipped_1",
            user_id="usr_shipped",
            total_amount=Decimal("1299.00"),
            status=OrderStatus.PAID,
            fulfillment_status="shipped",
            tracking_carrier="BlueDart",
            tracking_number="BD123456789IN",
            tracking_url="https://bluedart.com/track/BD123456789IN",
            shipping_name="Neha Sharma",
            shipping_line1="123 Park Street",
            shipping_city="Mumbai",
            shipping_state="Maharashtra",
            shipping_pincode="400001",
        )
        db.add(order)
        item = OrderLineItem(
            id="item_1",
            order_id="ord_shipped_1",
            vendor_id="ven_1",
            product_id="prd_1",
            product_name="Gourmet Chocolate Box",
            qty=1,
            unit_price_paise=129900,
            line_total_paise=129900,
        )
        db.add(item)
        await db.commit()

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            ok = await send_order_shipped_email(order, db)

            assert ok is True
            assert mock_send.call_count == 1
            call_kwargs = mock_send.call_args.kwargs
            assert call_kwargs["to_email"] == "aarav@example.com"
            assert "ord_shipped_1" in call_kwargs["subject"]
            assert "shipped" in call_kwargs["subject"].lower()
            assert "BlueDart" in call_kwargs["text"]
            assert "BD123456789IN" in call_kwargs["text"]
            assert "https://bluedart.com/track/BD123456789IN" in call_kwargs["text"]
            assert "Gourmet Chocolate Box" in call_kwargs["text"]
            assert "Track on Courier Site" in call_kwargs["html"]
            assert "View Live Timeline on Wishkr" in call_kwargs["html"]
            assert "/orders/track" in call_kwargs["html"]
            assert "/images/logo.png" in call_kwargs["html"]


@pytest.mark.asyncio
async def test_send_order_delivered_email(session_factory):
    async with session_factory() as db:
        user = User(id="usr_deliv", name="Priya Patel", email="priya@example.com")
        db.add(user)
        order = Order(
            id="ord_deliv_1",
            user_id="usr_deliv",
            total_amount=Decimal("899.00"),
            status=OrderStatus.PAID,
            fulfillment_status="delivered",
            shipping_name="Rohan Patel",
            shipping_line1="Flat 402, Sunshine Apts",
            shipping_city="Bengaluru",
            shipping_state="Karnataka",
            shipping_pincode="560034",
        )
        db.add(order)
        await db.commit()

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            ok = await send_order_delivered_email(order, db)

            assert ok is True
            assert mock_send.call_count == 1
            call_kwargs = mock_send.call_args.kwargs
            assert call_kwargs["to_email"] == "priya@example.com"
            assert "ord_deliv_1" in call_kwargs["subject"]
            assert "delivered" in call_kwargs["subject"].lower()
            assert "Rohan Patel" in call_kwargs["text"]
            assert "Sunshine Apts" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_send_order_cancelled_email(session_factory):
    async with session_factory() as db:
        user = User(id="usr_canc", name="Vikram Singh", email="vikram@example.com")
        db.add(user)
        order = Order(
            id="ord_canc_1",
            user_id="usr_canc",
            total_amount=Decimal("1500.00"),
            amount_collected_paise=150000,
            status=OrderStatus.CANCELLED,
        )
        db.add(order)
        await db.commit()

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            ok = await send_order_cancelled_email(
                order, db, reason="Recipient requested cancellation"
            )

            assert ok is True
            assert mock_send.call_count == 1
            call_kwargs = mock_send.call_args.kwargs
            assert call_kwargs["to_email"] == "vikram@example.com"
            assert "cancelled" in call_kwargs["subject"].lower()
            assert "Recipient requested cancellation" in call_kwargs["text"]
            assert "refund" in call_kwargs["text"].lower()


@pytest.mark.asyncio
async def test_send_order_refunded_email(session_factory):
    async with session_factory() as db:
        user = User(id="usr_ref", name="Ananya Das", email="ananya@example.com")
        db.add(user)
        order = Order(
            id="ord_ref_1",
            user_id="usr_ref",
            total_amount=Decimal("2499.00"),
            amount_collected_paise=249900,
            status=OrderStatus.REFUNDED,
        )
        db.add(order)
        await db.commit()

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            ok = await send_order_refunded_email(
                order, db, amount_rupees=Decimal("2499.00")
            )

            assert ok is True
            assert mock_send.call_count == 1
            call_kwargs = mock_send.call_args.kwargs
            assert call_kwargs["to_email"] == "ananya@example.com"
            assert "refund" in call_kwargs["subject"].lower()
            assert "2499.00" in call_kwargs["text"]
            assert "5–7 business days" in call_kwargs["text"] or "5-7 business days" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_update_fulfillment_triggers_shipped_email(session_factory):
    async with session_factory() as db:
        user = User(id="usr_full", name="Karan M", email="karan@example.com")
        db.add(user)
        order = Order(
            id="ord_full_1",
            user_id="usr_full",
            total_amount=Decimal("999.00"),
            status=OrderStatus.PAID,
            fulfillment_status="pending",
        )
        db.add(order)
        await db.commit()

        payload = FulfillmentPayload(
            fulfillment_status="shipped",
            tracking_carrier="Delhivery",
            tracking_number="DL12345",
            tracking_url="https://delhivery.com/track/DL12345",
        )

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            resp = await update_fulfillment("ord_full_1", payload, db=db, x_admin_email="ops@wishkr.com")

            assert resp["ok"] is True
            assert resp["fulfillment_status"] == "shipped"
            assert mock_send.call_count == 1
            assert mock_send.call_args.kwargs["to_email"] == "karan@example.com"
            assert "shipped" in mock_send.call_args.kwargs["subject"].lower()


@pytest.mark.asyncio
async def test_cancel_order_endpoint(session_factory):
    async with session_factory() as db:
        user = User(id="usr_cancel_api", name="Ritu V", email="ritu@example.com")
        db.add(user)
        order = Order(
            id="ord_cancel_api_1",
            user_id="usr_cancel_api",
            total_amount=Decimal("1999.00"),
            status=OrderStatus.PAID,
            fulfillment_status="pending",
        )
        db.add(order)
        await db.commit()

        payload = CancelOrderPayload(reason="Out of stock at vendor")

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            resp = await cancel_order(
                "ord_cancel_api_1",
                payload=payload,
                db=db,
                x_admin_email="ops@wishkr.com",
            )

            assert resp["ok"] is True
            assert resp["status"] == OrderStatus.CANCELLED.value
            assert resp["reason"] == "Out of stock at vendor"
            assert mock_send.call_count == 1
            assert "cancelled" in mock_send.call_args.kwargs["subject"].lower()
            assert "Out of stock at vendor" in mock_send.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_resend_fulfillment_notification(session_factory):
    async with session_factory() as db:
        user = User(id="usr_resend", name="Amit K", email="amit@example.com")
        db.add(user)
        order = Order(
            id="ord_resend_1",
            user_id="usr_resend",
            total_amount=Decimal("799.00"),
            status=OrderStatus.PAID,
            fulfillment_status="shipped",
            tracking_carrier="Delhivery",
            tracking_number="DL999",
        )
        db.add(order)
        await db.commit()

        with patch("app.services.order_notifications.send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            resp = await resend_fulfillment_notification("ord_resend_1", db=db)

            assert resp["ok"] is True
            assert resp["fulfillment_status"] == "shipped"
            assert mock_send.call_count == 1
            assert mock_send.call_args.kwargs["to_email"] == "amit@example.com"
