"""Post-purchase order editing: line-item recalculation, cross-vendor guard,
and freezing amount_collected_paise on the first edit of a pre-migration
order (one that was paid before this field existed).
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_orders import (
    CreateTopupPayload,
    EditOrderItemsPayload,
    _advance_creating_link,
    _outstanding_amount_paise,
    create_topup_link,
    edit_order_items,
    razorpay_service,
)
from app.api.product_orders import ProductOrderItemPayload
from app.db.base import Base
from app.models.admin_user import AuditLog
from app.models.order import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.order_payment_link import OrderPaymentLink
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.user import User
from app.models.vendor import Vendor


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                User.__table__,
                Vendor.__table__,
                Product.__table__,
                ProductVariant.__table__,
                ProductAddon.__table__,
                Order.__table__,
                OrderLineItem.__table__,
                OrderPaymentLink.__table__,
                AuditLog.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def _seed(db: AsyncSession) -> None:
    db.add(User(id="usr_1", phone="919000000003", name="Buyer"))
    db.add(Vendor(id="ven_1", name="Test Vendor", category="cake", active=True))
    db.add(
        Product(
            id="prd_1", vendor_id="ven_1", name="Cake", base_price_paise=69900,
            active=True, in_stock=True, variants=[], addons=[],
        )
    )
    db.add(
        ProductVariant(
            id="var_1kg", product_id="prd_1", name="1kg", price_delta_paise=109900,
            active=True, sort_order=1,
        )
    )
    db.add(
        Vendor(id="ven_2", name="Other Vendor", category="flowers", active=True)
    )
    db.add(
        Product(
            id="prd_2", vendor_id="ven_2", name="Roses", base_price_paise=89900,
            active=True, in_stock=True, variants=[], addons=[],
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_edit_order_items_recalculates_total(session_factory):
    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_1", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("400.00"), status=OrderStatus.PAID, order_type="product",
        )
        db.add(order)
        db.add(
            OrderLineItem(
                id="oli_1", order_id="ord_1", vendor_id="ven_1", product_id="prd_1",
                product_name="Product 1",
                qty=1, unit_price_paise=40000, line_total_paise=40000, shipping_fee_paise=0,
            )
        )
        await db.commit()

    async with session_factory() as db:
        result = await edit_order_items(
            "ord_1",
            EditOrderItemsPayload(
                items=[ProductOrderItemPayload(product_id="prd_1", variant_id="var_1kg", qty=1)]
            ),
            db,
            x_admin_email="ops@wishkr.com",
        )

    order_out = result["order"]
    assert order_out["total_amount"] == 1099.0
    assert order_out["vendor_amount_paise"] == 98910  # 109900 - round(109900*0.10)
    assert order_out["platform_amount_paise"] == 10990
    assert len(order_out["line_items"]) == 1
    assert order_out["line_items"][0]["unit_price_paise"] == 109900


@pytest.mark.asyncio
async def test_edit_order_items_rejects_cross_vendor(session_factory):
    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_2", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("699.00"), status=OrderStatus.PAID, order_type="product",
        )
        db.add(order)
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(Exception) as exc_info:
            await edit_order_items(
                "ord_2",
                EditOrderItemsPayload(
                    items=[ProductOrderItemPayload(product_id="prd_2", qty=1)]
                ),
                db,
                x_admin_email="ops@wishkr.com",
            )
        assert "different vendor" in str(exc_info.value)


@pytest.mark.asyncio
async def test_first_edit_freezes_amount_collected_for_pre_migration_order(session_factory):
    """Orders paid before amount_collected_paise existed have it NULL. The
    first edit must freeze it from the PRE-edit total, or the outstanding
    balance calculation silently drifts against the (about to change) live
    total_amount — this was caught manually against the seeded dev DB."""
    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_3", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("699.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=None,
        )
        db.add(order)
        db.add(
            OrderLineItem(
                id="oli_3", order_id="ord_3", vendor_id="ven_1", product_id="prd_1",
                product_name="Product 1",
                qty=1, unit_price_paise=69900, line_total_paise=69900, shipping_fee_paise=0,
            )
        )
        await db.commit()

    async with session_factory() as db:
        await edit_order_items(
            "ord_3",
            EditOrderItemsPayload(
                items=[ProductOrderItemPayload(product_id="prd_1", variant_id="var_1kg", qty=1)]
            ),
            db,
            x_admin_email="ops@wishkr.com",
        )

    async with session_factory() as db:
        order = await db.get(Order, "ord_3")
        assert order.amount_collected_paise == 69900  # frozen from PRE-edit total
        assert order.total_amount == Decimal("1099")

        outstanding = await _outstanding_amount_paise(db, order)
        assert outstanding == 109900 - 69900  # = 40000, not 0


@pytest.mark.asyncio
async def test_edit_order_items_rejects_non_product_order(session_factory):
    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_voucher", user_id="usr_1",
            total_amount=Decimal("500.00"), status=OrderStatus.PAID, order_type="voucher",
        )
        db.add(order)
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(Exception) as exc_info:
            await edit_order_items(
                "ord_voucher",
                EditOrderItemsPayload(items=[ProductOrderItemPayload(product_id="prd_1", qty=1)]),
                db,
                x_admin_email="ops@wishkr.com",
            )
        assert "product orders" in str(exc_info.value)


@pytest.mark.asyncio
async def test_edit_order_items_rejects_terminal_status(session_factory):
    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_cancelled", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("699.00"), status=OrderStatus.CANCELLED, order_type="product",
        )
        db.add(order)
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(Exception) as exc_info:
            await edit_order_items(
                "ord_cancelled",
                EditOrderItemsPayload(items=[ProductOrderItemPayload(product_id="prd_1", qty=1)]),
                db,
                x_admin_email="ops@wishkr.com",
            )
        assert "CANCELLED" in str(exc_info.value)


@pytest.mark.asyncio
async def test_second_topup_link_cancels_the_first(session_factory, monkeypatch):
    """Two simultaneously active (creating/created) top-up links for the same
    order would let a customer double-pay. Creating a second link must cancel
    the first, both on Razorpay and in our own record.

    create_topup_link now returns immediately with status='creating' — the
    Razorpay call + 'created' transition happens in _advance_creating_link,
    which this test drives explicitly for determinism (rather than the
    endpoint's fire-and-forget asyncio.create_task, which is disabled here
    via a no-op so it can't race the explicit calls below).
    _advance_creating_link opens its own session via app.db.session.async_session,
    so that name is monkeypatched to the test's in-memory sessionmaker.
    """
    import app.api.admin_orders as admin_orders_module
    import app.db.session as db_session_module

    created_links = []
    cancelled_ids = []

    async def fake_create_payment_link(**kwargs):
        link_id = f"plink_{len(created_links) + 1}"
        created_links.append(link_id)
        return {"id": link_id, "short_url": f"https://rzp.io/{link_id}"}

    async def fake_cancel_payment_link(link_id):
        cancelled_ids.append(link_id)
        return {"status": "cancelled"}

    real_create_task = asyncio.create_task

    def selective_create_task(coro, *args, **kwargs):
        # Drop only _advance_creating_link's fast-path attempt (identified by
        # its coroutine function) so the test's explicit calls below are the
        # sole driver; anything else (e.g. SQLAlchemy's own internal usage)
        # goes through normally.
        if getattr(coro, "cr_code", None) is admin_orders_module._advance_creating_link.__code__:
            coro.close()
            return real_create_task(_dummy_coro())
        return real_create_task(coro, *args, **kwargs)

    async def _dummy_coro():
        return None

    monkeypatch.setattr(razorpay_service, "create_payment_link", fake_create_payment_link)
    monkeypatch.setattr(razorpay_service, "cancel_payment_link", fake_cancel_payment_link)
    monkeypatch.setattr(db_session_module, "async_session", session_factory)
    monkeypatch.setattr(admin_orders_module.asyncio, "create_task", selective_create_task)

    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_topup", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("1099.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=69900,
        )
        db.add(order)
        await db.commit()

    async with session_factory() as db:
        first = await create_topup_link(
            "ord_topup", CreateTopupPayload(reason="first"), db, x_admin_email="ops@wishkr.com",
        )
    assert first["status"] == "creating"
    await _advance_creating_link(first["id"])

    async with session_factory() as db:
        second = await create_topup_link(
            "ord_topup", CreateTopupPayload(reason="second"), db, x_admin_email="ops@wishkr.com",
        )
    await _advance_creating_link(second["id"])

    assert first["id"] != second["id"]
    assert cancelled_ids == ["plink_1"]  # the first link was cancelled before the second was made

    async with session_factory() as db:
        links = (
            await db.execute(select(OrderPaymentLink).where(OrderPaymentLink.order_id == "ord_topup"))
        ).scalars().all()
    statuses = {link.id: link.status for link in links}
    assert statuses[first["id"]] == "cancelled"
    assert statuses[second["id"]] == "created"


@pytest.mark.asyncio
async def test_create_topup_link_returns_before_calling_razorpay(session_factory, monkeypatch):
    """create_topup_link's own transaction must not call Razorpay — only the
    detached _advance_creating_link (fast path or reconciliation job) does."""

    async def fail_if_called(**kwargs):
        raise AssertionError("create_payment_link must not be called synchronously")

    monkeypatch.setattr(razorpay_service, "create_payment_link", fail_if_called)

    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_fast", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("1099.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=69900,
        )
        db.add(order)
        await db.commit()

    async with session_factory() as db:
        result = await create_topup_link(
            "ord_fast", CreateTopupPayload(reason="fast"), db, x_admin_email="ops@wishkr.com",
        )
    assert result["status"] == "creating"

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, result["id"])
        assert row.status == "creating"
        assert row.razorpay_link_id is None


@pytest.mark.asyncio
async def test_advance_creating_link_transitions_to_created_or_failed(session_factory, monkeypatch):
    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "async_session", session_factory)

    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_advance", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("1099.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=69900,
        )
        db.add(order)
        db.add(
            OrderPaymentLink(
                id="opl_advance", order_id="ord_advance", amount_paise=40000, status="creating",
            )
        )
        await db.commit()

    async def fake_create_payment_link(**kwargs):
        return {"id": "plink_ok", "short_url": "https://rzp.io/ok"}

    monkeypatch.setattr(razorpay_service, "create_payment_link", fake_create_payment_link)
    await _advance_creating_link("opl_advance")

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_advance")
        assert row.status == "created"
        assert row.razorpay_short_url == "https://rzp.io/ok"

    # Now a row that fails on the Razorpay call.
    async with session_factory() as db:
        db.add(
            OrderPaymentLink(
                id="opl_fail", order_id="ord_advance", amount_paise=40000, status="creating",
            )
        )
        await db.commit()

    async def fake_create_payment_link_fails(**kwargs):
        raise RuntimeError("razorpay down")

    monkeypatch.setattr(razorpay_service, "create_payment_link", fake_create_payment_link_fails)
    await _advance_creating_link("opl_fail")

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_fail")
        assert row.status == "failed"
        assert row.retry_count == 1
        assert "razorpay down" in (row.last_error or "")


@pytest.mark.asyncio
async def test_editing_order_cancels_stale_open_topup_link(session_factory, monkeypatch):
    """An open (payable) top-up link is priced against the total at the time
    it was created. If the order is edited again afterward, that link is now
    wrong and must be cancelled immediately — not left live until someone
    happens to request a replacement."""
    cancelled_ids = []

    async def fake_cancel_payment_link(link_id):
        cancelled_ids.append(link_id)
        return {"status": "cancelled"}

    monkeypatch.setattr(razorpay_service, "cancel_payment_link", fake_cancel_payment_link)

    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_stale_link", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("1099.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=69900,
        )
        db.add(order)
        db.add(
            OrderLineItem(
                id="oli_stale", order_id="ord_stale_link", vendor_id="ven_1", product_id="prd_1",
                product_name="Product 1",
                variant_id="var_1kg", qty=1, unit_price_paise=109900, line_total_paise=109900,
                shipping_fee_paise=0,
            )
        )
        db.add(
            OrderPaymentLink(
                id="opl_open", order_id="ord_stale_link", amount_paise=40000, status="created",
                razorpay_link_id="plink_open",
            )
        )
        await db.commit()

    async with session_factory() as db:
        await edit_order_items(
            "ord_stale_link",
            EditOrderItemsPayload(
                items=[ProductOrderItemPayload(product_id="prd_1", variant_id="var_1kg", qty=2)]
            ),
            db,
            x_admin_email="ops@wishkr.com",
        )

    assert cancelled_ids == ["plink_open"]

    async with session_factory() as db:
        link = await db.get(OrderPaymentLink, "opl_open")
        assert link.status == "cancelled"


@pytest.mark.asyncio
async def test_advance_creating_link_claim_is_atomic_under_concurrency(session_factory, monkeypatch):
    """Two concurrent calls to _advance_creating_link for the SAME 'creating'
    row must result in exactly one Razorpay call — the phase-1 claim UPDATE
    (WHERE status='creating'), not timing, is what prevents a double call.
    This exercises the exact race the fast path (create_topup_link's
    asyncio.create_task) and the reconciliation job could hit if the first
    attempt takes longer than the job's stuck-row grace period."""
    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "async_session", session_factory)

    call_count = 0

    async def counting_create_payment_link(**kwargs):
        nonlocal call_count
        call_count += 1
        # Yield control so both concurrent calls have a chance to reach this
        # point before either finishes — the worst-case race window.
        await asyncio.sleep(0.01)
        return {"id": "plink_concurrent", "short_url": "https://rzp.io/concurrent"}

    monkeypatch.setattr(razorpay_service, "create_payment_link", counting_create_payment_link)

    async with session_factory() as db:
        await _seed(db)
        order = Order(
            id="ord_concurrent_claim", user_id="usr_1", vendor_id="ven_1",
            total_amount=Decimal("1099.00"), status=OrderStatus.PAID, order_type="product",
            amount_collected_paise=69900,
        )
        db.add(order)
        db.add(
            OrderPaymentLink(
                id="opl_concurrent", order_id="ord_concurrent_claim", amount_paise=40000,
                status="creating",
            )
        )
        await db.commit()

    await asyncio.gather(
        _advance_creating_link("opl_concurrent"),
        _advance_creating_link("opl_concurrent"),
    )

    assert call_count == 1, "only one concurrent caller should have won the claim and called Razorpay"

    async with session_factory() as db:
        row = await db.get(OrderPaymentLink, "opl_concurrent")
        assert row.status == "created"
        assert row.razorpay_short_url == "https://rzp.io/concurrent"
