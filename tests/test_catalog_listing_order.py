"""GET /api/catalog/products ordering.

Default storefront order is deterministic (never randomised/shuffled):
bestsellers first, then alphabetical by name within each group. Previously
this was pure alphabetical — is_bestseller was admin-settable and returned
in the payload but never affected ranking.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import catalog
from app.db.base import Base
from app.db.session import get_db
from app.models.admin_user import PricingSetting
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    Product.__table__,
                    ProductVariant.__table__,
                    ProductAddon.__table__,
                    Vendor.__table__,
                    PricingSetting.__table__,
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
    app.include_router(catalog.router, prefix="/api")

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


async def test_bestsellers_rank_before_non_bestsellers(client: TestClient, db: AsyncSession) -> None:
    vendor = Vendor(id="ven_1", name="Test Vendor", category="gifts", active=True)
    db.add(vendor)
    db.add_all(
        [
            Product(
                id="prd_zebra", vendor_id="ven_1", name="Zebra Print Mug",
                base_price_paise=10000, active=True, is_bestseller=True,
            ),
            Product(
                id="prd_apple", vendor_id="ven_1", name="Apple Candle",
                base_price_paise=20000, active=True, is_bestseller=False,
            ),
            Product(
                id="prd_daisy", vendor_id="ven_1", name="Daisy Vase",
                base_price_paise=30000, active=True, is_bestseller=True,
            ),
        ]
    )
    await db.commit()

    res = client.get("/api/catalog/products")

    assert res.status_code == 200
    names = [p["name"] for p in res.json()["products"]]
    # Both bestsellers ("Daisy Vase", "Zebra Print Mug") come first, sorted
    # alphabetically within the group, before the non-bestseller ("Apple
    # Candle") — proving rank isn't pure alphabetical (which would put Apple
    # first) and isn't random (this order is exact and reproducible).
    assert names == ["Daisy Vase", "Zebra Print Mug", "Apple Candle"]


async def test_equal_names_tie_break_by_id(client: TestClient, db: AsyncSession) -> None:
    vendor = Vendor(id="ven_1", name="Test Vendor", category="gifts", active=True)
    db.add(vendor)
    # Same name and bestseller flag — without the Product.id tie-breaker the
    # order of these two would be up to the database.
    db.add_all(
        [
            Product(
                id="prd_b", vendor_id="ven_1", name="Rose Bouquet",
                base_price_paise=20000, active=True, is_bestseller=False,
            ),
            Product(
                id="prd_a", vendor_id="ven_1", name="Rose Bouquet",
                base_price_paise=10000, active=True, is_bestseller=False,
            ),
        ]
    )
    await db.commit()

    res = client.get("/api/catalog/products")

    assert res.status_code == 200
    ids = [p["id"] for p in res.json()["products"]]
    assert ids == ["prd_a", "prd_b"]
