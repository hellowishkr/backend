"""Pytest fixtures for async database tests."""

import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.base import Base
from app.models import (
    User,
    Contact,
    WishlistItem,
    LlmUsageEvent,
    Occasion,
    RecipientPreference,
    ConversationMessage,
    ConversationState,
    GiftLog,
    CampaignContext,
    Reminder,
    OccasionGiftPlan,
    Product,
    Vendor,
    CelebrantOccasion,
    CelebrantFriend,
)
from app.models.recipient_preference import PreferenceRequest


# Only create tables used in tests
_TABLES = [
    User.__table__,
    Contact.__table__,
    WishlistItem.__table__,
    LlmUsageEvent.__table__,
    Occasion.__table__,
    RecipientPreference.__table__,
    ConversationMessage.__table__,
    ConversationState.__table__,
    PreferenceRequest.__table__,
    GiftLog.__table__,
    CampaignContext.__table__,
    Reminder.__table__,
    OccasionGiftPlan.__table__,
    Vendor.__table__,
    Product.__table__,
    CelebrantOccasion.__table__,
    CelebrantFriend.__table__,
]


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db() -> AsyncSession:
    """Provide an async database session with in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
        )

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with Session() as session:
        yield session

    await engine.dispose()
