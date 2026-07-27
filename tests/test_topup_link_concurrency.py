"""Proves the partial unique index (uq_order_active_payment_link) — not lock
timing — is what actually prevents two concurrently-active top-up payment
links for the same order.

Requires a live Postgres with the migration applied, pointed to via the
WISHKR_TEST_POSTGRES_URL env var — deliberately NOT settings.database_url,
which is the application's real configured DB. This test writes rows and
never cleans them up (see _seed_order's comment); running it against
whatever DATABASE_URL happens to resolve to in a given environment would
silently write test data into a real/shared database. Set
WISHKR_TEST_POSTGRES_URL explicitly to opt in:

    WISHKR_TEST_POSTGRES_URL=postgresql+asyncpg://wishkr:wishkr_dev@localhost:5432/wishkr \\
        pytest tests/test_topup_link_concurrency.py -m postgres

SQLite can't enforce a partial unique index the same way and all other tests
in this suite use in-memory SQLite — this is the one test that must run
against real Postgres to mean anything, hence the dedicated `postgres`
marker so it's skipped cleanly (not failed) wherever the env var isn't set
or the DB isn't reachable.
"""

import os
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.order import Order, OrderStatus
from app.models.order_payment_link import OrderPaymentLink
from app.models.user import User
from app.utils.id_gen import generate_id

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pg_session_factory():
    test_db_url = os.environ.get("WISHKR_TEST_POSTGRES_URL")
    if not test_db_url:
        pytest.skip(
            "WISHKR_TEST_POSTGRES_URL is not set — this test only means something "
            "against a real, dedicated Postgres, and is never run against the "
            "app's own configured DATABASE_URL"
        )

    engine = create_async_engine(test_db_url)
    try:
        async with engine.connect() as conn:
            pass  # prove the DB is actually reachable before running the test
    except Exception as exc:  # noqa: BLE001 — any connection failure (refused,
        # permission denied, DNS, etc.) should skip cleanly, not error the
        # suite; OperationalError alone missed e.g. sandboxed PermissionError.
        await engine.dispose()
        pytest.skip(f"Postgres at WISHKR_TEST_POSTGRES_URL is not reachable: {exc}")

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield Session
    finally:
        await engine.dispose()


async def _seed_order(Session: async_sessionmaker, order_id: str) -> None:
    # Random-suffixed phone — this test runs against the real, persistent
    # local dev Postgres, not a throwaway in-memory DB, so a fixed phone
    # number collides with User.phone's unique constraint across runs/tests.
    import random

    phone = f"9{random.randint(100000000, 999999999)}"
    async with Session() as db:
        db.add(User(id=f"usr_{order_id}", phone=phone, name="Concurrency Test Buyer"))
        db.add(
            Order(
                id=order_id, user_id=f"usr_{order_id}", total_amount=Decimal("1099.00"),
                status=OrderStatus.PAID, order_type="product", amount_collected_paise=69900,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_concurrent_active_link(pg_session_factory):
    """Two concurrent sessions each try to insert a 'creating' row for the
    same order. Exactly one must succeed; the other must fail with
    IntegrityError from uq_order_active_payment_link — proving the index,
    not application-level locking, is what makes 'at most one active link
    per order' true under real concurrency."""
    order_id = generate_id("ord")
    await _seed_order(pg_session_factory, order_id)

    async def _insert_creating_link(link_id: str) -> Exception | None:
        try:
            async with pg_session_factory() as db:
                db.add(
                    OrderPaymentLink(
                        id=link_id, order_id=order_id, amount_paise=40000, status="creating",
                    )
                )
                await db.commit()
            return None
        except IntegrityError as exc:
            return exc

    import asyncio

    link_a, link_b = generate_id("opl"), generate_id("opl")
    result_a, result_b = await asyncio.gather(
        _insert_creating_link(link_a), _insert_creating_link(link_b),
    )

    outcomes = [result_a, result_b]
    successes = [r for r in outcomes if r is None]
    failures = [r for r in outcomes if r is not None]

    assert len(successes) == 1, "exactly one concurrent insert should succeed"
    assert len(failures) == 1, "exactly one concurrent insert should fail on the unique index"
    assert isinstance(failures[0], IntegrityError)


@pytest.mark.asyncio
async def test_second_insert_succeeds_once_first_link_is_terminal(pg_session_factory):
    """Once the first link moves to a terminal-for-the-index state
    (cancelled), a new 'creating' row for the same order is allowed — the
    index only restricts 'creating'/'created', not the full lifetime."""
    order_id = generate_id("ord")
    await _seed_order(pg_session_factory, order_id)

    first_id = generate_id("opl")
    async with pg_session_factory() as db:
        db.add(
            OrderPaymentLink(id=first_id, order_id=order_id, amount_paise=40000, status="creating")
        )
        await db.commit()

    # Move it to cancelled — the index no longer covers this row.
    async with pg_session_factory() as db:
        link = await db.get(OrderPaymentLink, first_id)
        link.status = "cancelled"
        await db.commit()

    second_id = generate_id("opl")
    async with pg_session_factory() as db:
        db.add(
            OrderPaymentLink(id=second_id, order_id=order_id, amount_paise=40000, status="creating")
        )
        await db.commit()  # must not raise

    async with pg_session_factory() as db:
        row = await db.get(OrderPaymentLink, second_id)
        assert row.status == "creating"
