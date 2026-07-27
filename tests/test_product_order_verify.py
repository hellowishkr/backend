"""Tests for POST /api/orders/product/verify (Razorpay Checkout signature gate).

This endpoint never writes order status — the payment.captured webhook stays
the sole authority for marking an order PAID. Verify only confirms the
signature is genuine and reports current status, so the checkout UI doesn't
show success off an unverified browser callback.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import product_orders
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.models import Order, OrderStatus

_SECRET = "test_secret"


def _sign(order_id: str, payment_id: str) -> str:
    return hmac.new(_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _razorpay_secret(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_secret", _SECRET)


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Order.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db: AsyncSession) -> TestClient:
    app = FastAPI()
    app.include_router(product_orders.router, prefix="/api")

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


async def _make_order(db: AsyncSession, *, razorpay_order_id: str, status: OrderStatus) -> Order:
    order = Order(
        id="ord_test1",
        user_id="usr_test1",
        total_amount=Decimal("100.00"),
        currency="INR",
        status=status,
        order_type="product",
        razorpay_order_id=razorpay_order_id,
    )
    db.add(order)
    await db.commit()
    return order


async def test_valid_signature_reports_order_status(client: TestClient, db: AsyncSession) -> None:
    await _make_order(db, razorpay_order_id="order_rzp1", status=OrderStatus.PAID)
    signature = _sign("order_rzp1", "pay_rzp1")

    res = client.post(
        "/api/orders/product/verify",
        json={
            "razorpay_order_id": "order_rzp1",
            "razorpay_payment_id": "pay_rzp1",
            "razorpay_signature": signature,
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["verified"] is True
    assert body["order_id"] == "ord_test1"
    assert body["status"] == "PAID"
    assert body["confirmation_token"]


async def test_invalid_signature_rejected(client: TestClient, db: AsyncSession) -> None:
    await _make_order(db, razorpay_order_id="order_rzp2", status=OrderStatus.PENDING)

    res = client.post(
        "/api/orders/product/verify",
        json={
            "razorpay_order_id": "order_rzp2",
            "razorpay_payment_id": "pay_rzp2",
            "razorpay_signature": "not-the-real-signature",
        },
    )

    assert res.status_code == 400


async def test_unknown_order_returns_404(client: TestClient) -> None:
    signature = _sign("order_missing", "pay_missing")

    res = client.post(
        "/api/orders/product/verify",
        json={
            "razorpay_order_id": "order_missing",
            "razorpay_payment_id": "pay_missing",
            "razorpay_signature": signature,
        },
    )

    assert res.status_code == 404
