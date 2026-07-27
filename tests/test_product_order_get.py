"""Tests for GET /api/orders/product/{order_id} (public order confirmation lookup).

Authorised by a separate high-entropy confirmation_token, not the order id
alone — the id is only a timestamp plus 32 random bits, not enough entropy
to gate phone/address/gift-message/tracking behind. Must never leak vendor
payout fields (vendor_amount_paise, platform_amount_paise).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import product_orders
from app.db.base import Base
from app.db.session import get_db
from app.models import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.product import Product
from app.models.vendor import Vendor


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    Order.__table__,
                    OrderLineItem.__table__,
                    Product.__table__,
                    Vendor.__table__,
                ],
            )
        )
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


async def _make_order_with_item(
    db: AsyncSession, product_name: str = "Chocolate Cake", product_image_url: str | None = "https://example.com/cake.jpg"
) -> Order:
    vendor = Vendor(id="ven_test1", name="Test Vendor", category="cake")
    db.add(vendor)
    product = Product(
        id="prod_test1",
        vendor_id="ven_test1",
        name="Chocolate Cake",
        base_price_paise=50000,
        image_url="https://example.com/cake.jpg",
    )
    db.add(product)
    order = Order(
        id="ord_test1",
        confirmation_token="test-confirmation-token-1",
        user_id="usr_test1",
        total_amount=Decimal("500.00"),
        currency="INR",
        status=OrderStatus.PAID,
        order_type="product",
        vendor_id="ven_test1",
        shipping_name="Jane Doe",
        shipping_phone="9876543210",
        shipping_line1="221B Baker Street",
        shipping_city="Delhi",
        shipping_state="Delhi",
        shipping_pincode="110001",
        vendor_amount_paise=45000,
        platform_amount_paise=5000,
    )
    db.add(order)
    await db.flush()
    db.add(
        OrderLineItem(
            id="oli_test1",
            order_id=order.id,
            vendor_id="ven_test1",
            product_id="prod_test1",
            product_name=product_name,
            product_image_url=product_image_url,
            selected_addons=[],
            customizations={},
            qty=1,
            unit_price_paise=50000,
            line_total_paise=50000,
            shipping_fee_paise=0,
        )
    )
    await db.commit()
    return order


async def test_get_order_returns_buyer_facing_fields(client: TestClient, db: AsyncSession) -> None:
    await _make_order_with_item(db)

    res = client.get("/api/orders/product/ord_test1?token=test-confirmation-token-1")

    assert res.status_code == 200
    body = res.json()
    assert body["order_id"] == "ord_test1"
    assert body["status"] == "PAID"
    assert body["shipping_address"]["city"] == "Delhi"
    assert len(body["items"]) == 1
    assert body["items"][0]["product_name"] == "Chocolate Cake"
    assert body["items"][0]["image_url"] == "https://example.com/cake.jpg"
    assert "vendor_amount_paise" not in body
    assert "platform_amount_paise" not in body


async def test_get_order_wrong_token_returns_404(client: TestClient, db: AsyncSession) -> None:
    """Regression (P1): the order id alone must not be sufficient to view
    PII (phone/address/gift message/tracking) — only the separate
    high-entropy confirmation_token authorises this lookup."""
    await _make_order_with_item(db)

    res = client.get("/api/orders/product/ord_test1?token=not-the-real-token")

    assert res.status_code == 404


async def test_get_order_missing_token_returns_422(client: TestClient, db: AsyncSession) -> None:
    await _make_order_with_item(db)

    res = client.get("/api/orders/product/ord_test1")

    assert res.status_code == 422


async def test_get_unknown_order_returns_404(client: TestClient) -> None:
    res = client.get("/api/orders/product/ord_missing?token=whatever")
    assert res.status_code == 404


async def test_confirmation_shows_product_name_at_order_time_not_current(
    client: TestClient, db: AsyncSession
) -> None:
    """Regression (P2): product_name/image_url must be read from the
    OrderLineItem snapshot, not joined live from the current Product row —
    otherwise a vendor renaming/re-imaging a product later rewrites what a
    past confirmation page shows, and deleting the product blanks it out
    entirely instead of showing what the buyer actually bought."""
    await _make_order_with_item(db, product_name="Chocolate Cake (Diwali Special)")

    # Vendor renames the live product after the order was placed.
    product = await db.get(Product, "prod_test1")
    product.name = "Chocolate Cake (Renamed)"
    product.image_url = "https://example.com/new-photo.jpg"
    await db.commit()

    res = client.get("/api/orders/product/ord_test1?token=test-confirmation-token-1")

    assert res.status_code == 200
    body = res.json()
    assert body["items"][0]["product_name"] == "Chocolate Cake (Diwali Special)"
    assert body["items"][0]["image_url"] == "https://example.com/cake.jpg"


async def test_get_non_product_order_returns_404(client: TestClient, db: AsyncSession) -> None:
    order = Order(
        id="ord_voucher1",
        confirmation_token="test-confirmation-token-2",
        user_id="usr_test1",
        total_amount=Decimal("100.00"),
        currency="INR",
        status=OrderStatus.PAID,
        order_type="voucher",
    )
    db.add(order)
    await db.commit()

    res = client.get("/api/orders/product/ord_voucher1?token=test-confirmation-token-2")
    assert res.status_code == 404
