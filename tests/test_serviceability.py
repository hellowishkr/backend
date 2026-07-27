import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.city import City
from app.utils.serviceability import check_product_serviceability


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[City.__table__])
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _pune(db: AsyncSession) -> None:
    db.add(City(id="pune", name="Pune", aliases=[], pincode_prefixes=["411"]))
    await db.commit()


@pytest.mark.asyncio
async def test_pincode_only_rejects_wrong_pincode(session_factory):
    """A correct city NAME must not rescue a pincode outside coverage."""
    async with session_factory() as db:
        await _pune(db)
        # dest_city omitted (None) — order path now matches on pincode only.
        result = await check_product_serviceability(
            db,
            ships_nationally=False,
            vendor_city_ids=["pune"],
            dest_city=None,
            dest_pincode="511018",  # Telangana prefix, not 411xxx
        )
        assert result.serviceable is False


@pytest.mark.asyncio
async def test_pincode_in_prefix_is_serviceable(session_factory):
    async with session_factory() as db:
        await _pune(db)
        result = await check_product_serviceability(
            db,
            ships_nationally=False,
            vendor_city_ids=["pune"],
            dest_city=None,
            dest_pincode="411001",
        )
        assert result.serviceable is True


@pytest.mark.asyncio
async def test_national_always_serviceable(session_factory):
    async with session_factory() as db:
        result = await check_product_serviceability(
            db,
            ships_nationally=True,
            vendor_city_ids=[],
            dest_city=None,
            dest_pincode="511018",
        )
        assert result.serviceable is True
