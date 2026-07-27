"""Product Razorpay Checkout webhook state transitions."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.order import Order, OrderStatus
from app.webhooks import razorpay_handlers


def _payload(*, order_id: str | None = "ord_product", payment_id: str = "pay_product") -> dict:
    notes = {"order_id": order_id} if order_id else {}
    return {
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "notes": notes,
                }
            }
        }
    }


def _order(*, status: OrderStatus = OrderStatus.PENDING, order_type: str = "product") -> Order:
    return Order(
        id="ord_product",
        user_id="usr_product",
        total_amount=Decimal("1299.00"),
        status=status,
        order_type=order_type,
    )


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_product_payment_captured_marks_order_paid(monkeypatch):
    order = _order()
    db = _db()
    notify = AsyncMock()
    monkeypatch.setattr(razorpay_handlers, "_get_order", AsyncMock(return_value=order))
    monkeypatch.setattr(razorpay_handlers, "_notify_vendor_of_product_order", notify)

    result = await razorpay_handlers.handle_payment_captured(_payload(), db)

    assert result == {"status": "ok", "order_id": order.id}
    assert order.status == OrderStatus.PAID
    assert order.razorpay_payment_id == "pay_product"
    assert order.amount_collected_paise == 129900
    db.add.assert_called_once_with(order)
    assert db.commit.await_count == 2
    notify.assert_awaited_once_with(order, db)


@pytest.mark.asyncio
async def test_product_payment_captured_is_idempotent(monkeypatch):
    order = _order(status=OrderStatus.PAID)
    order.razorpay_payment_id = "pay_product"
    db = _db()
    monkeypatch.setattr(razorpay_handlers, "_get_order", AsyncMock(return_value=order))

    result = await razorpay_handlers.handle_payment_captured(_payload(), db)

    assert result == {"status": "already_processed", "order_id": order.id}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_payment_captured_ignores_unknown_and_non_product_orders(monkeypatch):
    db = _db()
    get_order = AsyncMock(return_value=None)
    monkeypatch.setattr(razorpay_handlers, "_get_order", get_order)

    unknown = await razorpay_handlers.handle_payment_captured(_payload(order_id="ord_missing"), db)
    assert unknown == {"status": "order_not_found", "order_id": "ord_missing"}

    get_order.return_value = _order(order_type="voucher")
    ignored = await razorpay_handlers.handle_payment_captured(_payload(), db)
    assert ignored == {"status": "ignored_not_product_order", "order_id": "ord_product"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_payment_captured_rejects_invalid_state(monkeypatch):
    order = _order(status=OrderStatus.CANCELLED)
    db = _db()
    monkeypatch.setattr(razorpay_handlers, "_get_order", AsyncMock(return_value=order))

    result = await razorpay_handlers.handle_payment_captured(_payload(), db)

    assert result == {
        "status": "invalid_state",
        "order_id": order.id,
        "current": OrderStatus.CANCELLED.value,
    }
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_state_is_committed_before_vendor_notification(monkeypatch):
    order = _order()
    db = _db()
    monkeypatch.setattr(razorpay_handlers, "_get_order", AsyncMock(return_value=order))
    monkeypatch.setattr(
        razorpay_handlers,
        "_notify_vendor_of_product_order",
        AsyncMock(side_effect=RuntimeError("notification unavailable")),
    )

    with pytest.raises(RuntimeError, match="notification unavailable"):
        await razorpay_handlers.handle_payment_captured(_payload(), db)

    assert order.status == OrderStatus.PAID
    assert order.razorpay_payment_id == "pay_product"
    assert db.commit.await_count == 1
