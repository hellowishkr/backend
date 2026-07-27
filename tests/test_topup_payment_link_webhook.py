"""Top-up payment link webhook routing must never collide with the order's
original payment. reference_id="opl_..." (never "ord_...") routes
payment_link.paid/expired/cancelled to the OrderPaymentLink-only handlers,
which touch nothing on Order itself.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.order import Order, OrderStatus
from app.models.order_payment_link import OrderPaymentLink
from app.models.user import User
from app.webhooks import razorpay_handlers


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


async def _seed_order(db: AsyncSession) -> Order:
    user = User(id="usr_topup", phone="919000000002", name="Test Buyer")
    order = Order(
        id="ord_topup_test",
        user_id=user.id,
        total_amount=Decimal("1848.00"),
        status=OrderStatus.PAID,
        order_type="product",
        razorpay_payment_id="pay_original_123",
        amount_collected_paise=184800,
    )
    db.add_all([user, order])
    await db.commit()
    return order


@pytest.mark.asyncio
async def test_topup_link_paid_updates_only_the_link_not_the_order(session_factory):
    async with session_factory() as db:
        order = await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_1",
            order_id=order.id,
            amount_paise=45000,
            status="created",
            razorpay_link_id="plink_abc",
            razorpay_short_url="https://rzp.io/rzp/test",
        )
        db.add(opl)
        await db.commit()

    payload = {
        "payload": {
            "payment_link": {"entity": {"reference_id": "opl_test_1"}},
            "payment": {"entity": {"id": "pay_topup_456"}},
        }
    }

    async with session_factory() as db:
        result = await razorpay_handlers.handle_payment_link_paid(payload, db)

        refreshed_opl = await db.get(OrderPaymentLink, "opl_test_1")
        refreshed_order = await db.get(Order, "ord_topup_test")

    assert result["status"] == "ok"
    assert result["order_id"] == "ord_topup_test"
    assert refreshed_opl.status == "paid"
    assert refreshed_opl.razorpay_payment_id == "pay_topup_456"
    assert refreshed_opl.paid_at is not None

    # Critical non-collision assertion: the ORIGINAL order payment fields must
    # be completely untouched by the top-up webhook.
    assert refreshed_order.razorpay_payment_id == "pay_original_123"
    assert refreshed_order.status == OrderStatus.PAID
    assert refreshed_order.amount_collected_paise == 184800


@pytest.mark.asyncio
async def test_topup_link_paid_is_idempotent(session_factory):
    async with session_factory() as db:
        await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_2", order_id="ord_topup_test", amount_paise=10000, status="created",
        )
        db.add(opl)
        await db.commit()

    payload = {
        "payload": {
            "payment_link": {"entity": {"reference_id": "opl_test_2"}},
            "payment": {"entity": {"id": "pay_topup_789"}},
        }
    }

    async with session_factory() as db:
        first = await razorpay_handlers.handle_payment_link_paid(payload, db)
    async with session_factory() as db:
        second = await razorpay_handlers.handle_payment_link_paid(payload, db)

    assert first["status"] == "ok"
    assert second["status"] == "already_processed"


@pytest.mark.asyncio
async def test_topup_link_expired_routes_correctly(session_factory):
    async with session_factory() as db:
        await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_3", order_id="ord_topup_test", amount_paise=5000, status="created",
        )
        db.add(opl)
        await db.commit()

    payload = {"payload": {"payment_link": {"entity": {"reference_id": "opl_test_3"}}}}

    async with session_factory() as db:
        result = await razorpay_handlers.handle_payment_link_expired(payload, db)
        refreshed_opl = await db.get(OrderPaymentLink, "opl_test_3")
        refreshed_order = await db.get(Order, "ord_topup_test")

    assert result["status"] == "ok"
    assert refreshed_opl.status == "expired"
    # Order's own status (PAID) must be untouched — expiry of a top-up link is
    # not the same as the original payment link expiring.
    assert refreshed_order.status == OrderStatus.PAID


@pytest.mark.asyncio
async def test_topup_link_cancelled_accepts_cancel_pending(session_factory):
    """A link in cancel_pending means our own cancel attempt is in flight or
    hasn't committed yet. The webhook confirming cancellation must still land
    the row in its terminal state, not silently ignore it (which would leave
    it stuck at cancel_pending forever, even though Razorpay-side truth is
    already terminal)."""
    async with session_factory() as db:
        await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_4", order_id="ord_topup_test", amount_paise=5000,
            status="cancel_pending", razorpay_link_id="plink_pending",
        )
        db.add(opl)
        await db.commit()

    payload = {"payload": {"payment_link": {"entity": {"reference_id": "opl_test_4"}}}}

    async with session_factory() as db:
        result = await razorpay_handlers.handle_payment_link_cancelled(payload, db)
        refreshed_opl = await db.get(OrderPaymentLink, "opl_test_4")

    assert result["status"] == "ok"
    assert refreshed_opl.status == "cancelled"


@pytest.mark.asyncio
async def test_topup_link_expired_accepts_cancel_pending(session_factory):
    async with session_factory() as db:
        await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_5", order_id="ord_topup_test", amount_paise=5000,
            status="cancel_pending", razorpay_link_id="plink_pending2",
        )
        db.add(opl)
        await db.commit()

    payload = {"payload": {"payment_link": {"entity": {"reference_id": "opl_test_5"}}}}

    async with session_factory() as db:
        result = await razorpay_handlers.handle_payment_link_expired(payload, db)
        refreshed_opl = await db.get(OrderPaymentLink, "opl_test_5")

    assert result["status"] == "ok"
    assert refreshed_opl.status == "expired"


@pytest.mark.asyncio
async def test_topup_link_paid_is_untouched_by_stray_terminal_webhook(session_factory):
    """A link already 'paid' must not be flipped by a late-arriving
    cancelled/expired webhook (e.g. a race between the customer paying and an
    admin's cancel attempt)."""
    async with session_factory() as db:
        await _seed_order(db)
        opl = OrderPaymentLink(
            id="opl_test_6", order_id="ord_topup_test", amount_paise=5000,
            status="paid", razorpay_payment_id="pay_already_paid",
        )
        db.add(opl)
        await db.commit()

    payload = {"payload": {"payment_link": {"entity": {"reference_id": "opl_test_6"}}}}

    async with session_factory() as db:
        result = await razorpay_handlers.handle_payment_link_cancelled(payload, db)
        refreshed_opl = await db.get(OrderPaymentLink, "opl_test_6")

    assert result["status"] == "ok"
    assert refreshed_opl.status == "paid"  # untouched — guard only matches created/cancel_pending
