"""Admin pricing endpoint: returns cleanly after commit (no expired-attribute
lazy-load 500), and rejects financially/DB-invalid configurations."""

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_settings import PricingPayload, get_pricing, update_pricing
from app.db.base import Base
from app.models.admin_user import AuditLog, PricingSetting


@pytest.fixture
async def pricing_db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[PricingSetting.__table__, AuditLog.__table__],
        )
    # The app session uses expire_on_commit=False; this test forces =True, which
    # is STRICTER — it guarantees the row's attributes are expired after commit,
    # so touching updated_at (onupdate=func.now()) without a refresh would trigger
    # the async lazy-load that raised MissingGreenlet. Passing here means the
    # refresh fix holds even under the harsher expiry than production runs.
    Session = async_sessionmaker(engine, expire_on_commit=True, class_=AsyncSession)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_update_pricing_returns_after_commit(pricing_db: AsyncSession) -> None:
    payload = PricingPayload(markup_pct=30, flash_off_pct=20, flash_active=True)
    result = await update_pricing(payload, pricing_db)  # must not raise
    assert result["pricing"]["markup_pct"] == 30.0
    assert result["pricing"]["flash_off_pct"] == 20.0
    assert result["pricing"]["flash_active"] is True
    assert result["pricing"]["updated_at"] is not None  # timestamp survived


@pytest.mark.asyncio
async def test_get_pricing_reflects_update(pricing_db: AsyncSession) -> None:
    await update_pricing(
        PricingPayload(markup_pct=0, flash_off_pct=0, flash_active=False), pricing_db
    )
    got = await get_pricing(pricing_db)
    assert got["pricing"]["flash_active"] is False
    assert got["pricing"]["markup_pct"] == 0.0


def test_flash_exceeding_markup_rejected() -> None:
    # markup 0 + flash 100 would charge ₹0 and make platform_amount negative.
    with pytest.raises(ValidationError):
        PricingPayload(markup_pct=0, flash_off_pct=100, flash_active=True)
    # Even a partial: 10% markup vs 20% off ⇒ charge 0.88·V < V ⇒ negative platform.
    with pytest.raises(ValidationError):
        PricingPayload(markup_pct=10, flash_off_pct=20, flash_active=True)


def test_markup_beyond_numeric_column_rejected() -> None:
    # NUMERIC(5,2) tops out at 999.99; 1000 must not pass validation.
    with pytest.raises(ValidationError):
        PricingPayload(markup_pct=1000, flash_off_pct=0, flash_active=True)


def test_disabled_flash_allows_any_markup_vs_flash() -> None:
    # With flash off, the flash % is irrelevant — no charge-below-vendor risk.
    p = PricingPayload(markup_pct=0, flash_off_pct=90, flash_active=False)
    assert p.flash_active is False
