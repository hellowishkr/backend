"""Reconciliation job for stuck/failed top-up payment-link creations and
cancellations — the correctness backstop behind create_topup_link's
best-effort in-request fast path (app.api.admin_orders._advance_creating_link).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.jobs.topup_link_reconciliation as reconciliation_module
from app.db.base import Base
from app.jobs.topup_link_reconciliation import topup_link_reconciliation
from app.models.order import Order, OrderStatus
from app.models.order_payment_link import OrderPaymentLink
from app.models.user import User
from app.services.razorpay_service import razorpay_service


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, Order.__table__, OrderPaymentLink.__table__],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed_order(db: AsyncSession, order_id: str = "ord_reconcile") -> None:
    db.add(User(id=f"usr_{order_id}", phone="919000000004", name="Buyer"))
    db.add(
        Order(
            id=order_id, user_id=f"usr_{order_id}", total_amount=Decimal("1099.00"),
            status=OrderStatus.PAID, order_type="product", amount_collected_paise=69900,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_stuck_creating_row_advances_to_created(session_factory, monkeypatch):
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)
    import app.db.session as db_session_module
    monkeypatch.setattr(db_session_module, "async_session", session_factory)

    stale_created_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    async with session_factory() as db:
        await _seed_order(db)
        db.add(
            OrderPaymentLink(
                id="opl_stuck", order_id="ord_reconcile", amount_paise=40000,
                status="creating", created_at=stale_created_at, state_changed_at=stale_created_at,
            )
        )
        await db.commit()

    async def fake_create_payment_link(**kwargs):
        return {"id": "plink_recovered", "short_url": "https://rzp.io/recovered"}

    monkeypatch.setattr(razorpay_service, "create_payment_link", fake_create_payment_link)

    await topup_link_reconciliation()

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_stuck")
        assert row.status == "created"
        assert row.razorpay_short_url == "https://rzp.io/recovered"


@pytest.mark.asyncio
async def test_recent_creating_row_is_not_touched(session_factory, monkeypatch):
    """A 'creating' row younger than the grace period is presumed still
    legitimately in flight — the job must not race the fast path."""
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)

    async def fail_if_called(**kwargs):
        raise AssertionError("should not call Razorpay for a fresh 'creating' row")

    monkeypatch.setattr(razorpay_service, "create_payment_link", fail_if_called)

    async with session_factory() as db:
        await _seed_order(db)
        db.add(
            OrderPaymentLink(
                id="opl_fresh", order_id="ord_reconcile", amount_paise=40000, status="creating",
            )
        )
        await db.commit()

    await topup_link_reconciliation()

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_fresh")
        assert row.status == "creating"


@pytest.mark.asyncio
async def test_failed_row_is_retried_with_backoff(session_factory, monkeypatch):
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)
    import app.db.session as db_session_module
    monkeypatch.setattr(db_session_module, "async_session", session_factory)

    stale_created_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    async with session_factory() as db:
        await _seed_order(db)
        db.add(
            OrderPaymentLink(
                id="opl_failed", order_id="ord_reconcile", amount_paise=40000,
                status="failed", retry_count=0, created_at=stale_created_at,
                state_changed_at=stale_created_at,
            )
        )
        await db.commit()

    async def fake_create_payment_link(**kwargs):
        return {"id": "plink_retry_ok", "short_url": "https://rzp.io/retry-ok"}

    monkeypatch.setattr(razorpay_service, "create_payment_link", fake_create_payment_link)

    await topup_link_reconciliation()

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_failed")
        assert row.status == "created"


@pytest.mark.asyncio
async def test_failed_row_exhausted_retries_is_not_retried(session_factory, monkeypatch):
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)

    async def fail_if_called(**kwargs):
        raise AssertionError("should not retry a row past _MAX_RETRIES")

    monkeypatch.setattr(razorpay_service, "create_payment_link", fail_if_called)

    stale_created_at = datetime.now(timezone.utc) - timedelta(seconds=200)
    async with session_factory() as db:
        await _seed_order(db)
        db.add(
            OrderPaymentLink(
                id="opl_exhausted", order_id="ord_reconcile", amount_paise=40000,
                status="failed", retry_count=3, created_at=stale_created_at,
                state_changed_at=stale_created_at,
            )
        )
        await db.commit()

    await topup_link_reconciliation()

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_exhausted")
        assert row.status == "failed"
        assert row.retry_count == 3


@pytest.mark.asyncio
async def test_stuck_cancel_pending_row_retries_cancel(session_factory, monkeypatch):
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)

    cancelled_ids = []

    async def fake_cancel_payment_link(link_id):
        cancelled_ids.append(link_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(razorpay_service, "cancel_payment_link", fake_cancel_payment_link)

    stale_created_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    async with session_factory() as db:
        await _seed_order(db)
        db.add(
            OrderPaymentLink(
                id="opl_cancel_stuck", order_id="ord_reconcile", amount_paise=40000,
                status="cancel_pending", razorpay_link_id="plink_stuck_cancel",
                created_at=stale_created_at, state_changed_at=stale_created_at,
            )
        )
        await db.commit()

    await topup_link_reconciliation()

    assert cancelled_ids == ["plink_stuck_cancel"]
    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_cancel_stuck")
        assert row.status == "cancelled"


@pytest.mark.asyncio
async def test_dlq_alert_fires_once_threshold_reached(session_factory, monkeypatch):
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)
    monkeypatch.setattr(reconciliation_module, "_last_dlq_alert_ts", 0.0)

    alerts = []

    async def fake_alert(message):
        alerts.append(message)

    monkeypatch.setattr(reconciliation_module, "send_founder_alert", fake_alert)

    async with session_factory() as db:
        await _seed_order(db)
        for i in range(reconciliation_module._DLQ_ALERT_THRESHOLD):
            db.add(
                OrderPaymentLink(
                    id=f"opl_dlq_{i}", order_id="ord_reconcile", amount_paise=1000,
                    status="failed", retry_count=reconciliation_module._MAX_RETRIES,
                )
            )
        await db.commit()

    await topup_link_reconciliation()

    assert len(alerts) == 1
    assert "DLQ" in alerts[0]


@pytest.mark.asyncio
async def test_failed_row_skips_retry_if_order_already_settled(session_factory, monkeypatch):
    """A 'failed' row must not be resurrected if the order's outstanding
    balance has since dropped to zero (e.g. a newer top-up link already got
    paid, or an edit reduced what's owed) — retrying unconditionally would
    send a second payment request for money already collected."""
    monkeypatch.setattr(reconciliation_module, "async_session", session_factory)

    async def fail_if_called(**kwargs):
        raise AssertionError("must not call Razorpay for a settled order")

    monkeypatch.setattr(razorpay_service, "create_payment_link", fail_if_called)

    stale_created_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    async with session_factory() as db:
        # amount_collected_paise == total_amount * 100 -> outstanding is 0.
        db.add(User(id="usr_settled", phone="919000000006", name="Buyer"))
        db.add(
            Order(
                id="ord_settled", user_id="usr_settled", total_amount=Decimal("1099.00"),
                status=OrderStatus.PAID, order_type="product", amount_collected_paise=109900,
            )
        )
        db.add(
            OrderPaymentLink(
                id="opl_stale_failed", order_id="ord_settled", amount_paise=40000,
                status="failed", retry_count=0, created_at=stale_created_at,
                state_changed_at=stale_created_at,
            )
        )
        await db.commit()

    await topup_link_reconciliation()

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_stale_failed")
        assert row.status == "cancelled"
        assert "settled" in (row.last_error or "")
