"""GET /api/catalog/taxonomy — DB-backed categories and occasions.

Redis is not configured in tests (settings.redis_url empty → get_redis()
returns None), so these exercise the DB fallback path directly.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import catalog
from app.db.base import Base
from app.db.session import get_db
from app.models.catalog_taxonomy import GiftingOccasion, ProductCategory


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[ProductCategory.__table__, GiftingOccasion.__table__],
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


async def test_taxonomy_returns_seeded_rows_in_sort_order(
    client: TestClient, db: AsyncSession
) -> None:
    db.add_all(
        [
            ProductCategory(id="cakes", name="Cakes", sort_order=1),
            ProductCategory(id="flowers", name="Flowers", sort_order=0),
            GiftingOccasion(id="wedding", name="Wedding", sort_order=2),
            GiftingOccasion(id="birthday", name="Birthday", sort_order=0),
            GiftingOccasion(id="anniversary", name="Anniversary", sort_order=1),
        ]
    )
    await db.commit()

    res = client.get("/api/catalog/taxonomy")

    assert res.status_code == 200
    body = res.json()
    assert body["categories"] == ["Flowers", "Cakes"]
    assert body["occasions"] == [
        {"label": "Birthday", "slug": "birthday"},
        {"label": "Anniversary", "slug": "anniversary"},
        {"label": "Wedding", "slug": "wedding"},
    ]
