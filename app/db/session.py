"""Database engine and session factories."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

_ENGINE_KWARGS = {
    # SQL echo is opt-in via DB_ECHO_SQL — default off. Previously tied to
    # app_env=development, which made dev logs ~85% SQL noise from the
    # 15s retry_failed_outbound cron alone. Flip on temporarily when
    # debugging a query path.
    "echo": settings.db_echo_sql,
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    # Post-cutover / post-restart safety: make stale connections self-heal.
    "pool_pre_ping": True,
    # Recycle sockets periodically so long-lived workers don't sit on
    # dead connections after a DB restart or transient network flap.
    "pool_recycle": 300,
}


def create_resilient_async_engine(db_url: str | None = None) -> AsyncEngine:
    """Create the app's async SQLAlchemy engine with reconnect safeguards."""
    return create_async_engine(db_url or settings.database_url, **_ENGINE_KWARGS)


# Async engine (asyncpg)
engine = create_resilient_async_engine(settings.database_url)

# Async session factory
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with async_session() as session:
        yield session
