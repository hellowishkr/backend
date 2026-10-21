"""Refund acknowledgements and real PostgreSQL deduplication/worker races."""

import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.core.config import settings
from app.db.base import Base
from app.jobs import process_razorpay_refunds as worker
from app.models import Order, OrderStatus, User, WebhookEvent, WebhookEventStatus, WebhookProvider
from app.models import Purchase
from app.services import order_notifications
from app.webhooks import razorpay
from app.webhooks import razorpay_handlers as handlers


def _payload(event="refund.processed", refund_id="rfnd_test"):
    return {
        "event": event,
        "payload": {
            "refund": {"entity": {"id": refund_id, "payment_id": "pay_test", "amount": 10000}}
        },
    }


def _request(payload, event_id="evt_test"):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    headers = [(b"x-razorpay-signature", b"test-signature")]
    if event_id is not None:
        headers.append((b"x-razorpay-event-id", event_id.encode()))
    return Request({"type": "http", "headers": headers}, receive)


@pytest.fixture(autouse=True)
def verified(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-secret")
    monkeypatch.setattr(razorpay.razorpay_service, "verify_webhook_signature", lambda *_: True)


@pytest.mark.parametrize("event", worker.REFUND_HANDLERS)
async def test_refunds_commit_before_acknowledging_without_running_handler(event, monkeypatch):
    db = MagicMock(flush=AsyncMock(), commit=AsyncMock())
    handler = AsyncMock(side_effect=AssertionError("must not send during request"))
    monkeypatch.setitem(razorpay.EVENT_HANDLERS, event, (handler, lambda _: None))
    payload = _payload(event)
    payload["id"] = "wrong-body-id"

    result = await razorpay.razorpay_webhook(_request(payload), db)

    assert result == {"status": "accepted", "event": event}
    audit = db.add.call_args.args[0]
    assert audit.event_id == "evt_test"
    assert audit.status == WebhookEventStatus.RECEIVED
    db.commit.assert_awaited_once()
    handler.assert_not_awaited()


async def test_invalid_signature_cannot_reserve_legitimate_event_id(monkeypatch):
    monkeypatch.setattr(razorpay.razorpay_service, "verify_webhook_signature", lambda *_: False)
    db = MagicMock(commit=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await razorpay.razorpay_webhook(_request(_payload()), db)
    assert exc.value.status_code == 400
    assert db.add.call_args.args[0].event_id is None


@pytest.fixture
async def pg(monkeypatch):
    # Explicit opt-in: never use the application's configured production DB.
    url = os.environ.get("WISHKR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("WISHKR_TEST_POSTGRES_URL requires a dedicated test PostgreSQL")
    schema = "refund_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            tables = {WebhookEvent.__table__, Order.__table__, Purchase.__table__}
            pending = list(tables)
            while pending:
                for fk in pending.pop().foreign_keys:
                    if fk.column.table not in tables:
                        tables.add(fk.column.table)
                        pending.append(fk.column.table)
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
            )
        monkeypatch.setattr(worker, "async_session", factory)
        async with factory() as db:
            db.add(User(id="usr_test", phone="919000000000", name="Test Buyer"))
            await db.flush()
            db.add(
                Order(
                    id="ord_test",
                    user_id="usr_test",
                    total_amount=Decimal("100.00"),
                    razorpay_payment_id="pay_test",
                    status=OrderStatus.PAID,
                )
            )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
def notifications(monkeypatch):
    customer = AsyncMock()
    founder = AsyncMock()
    email = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "send_text_or_template", customer)
    monkeypatch.setattr(handlers, "send_founder_alert", founder)
    monkeypatch.setattr(order_notifications, "send_order_refunded_email", email)
    return customer, founder, email


async def _receive(pg, *, event="refund.processed", event_id="evt_test", refund_id="rfnd_test"):
    async with pg() as db:
        return await razorpay.razorpay_webhook(_request(_payload(event, refund_id), event_id), db)


@pytest.mark.postgres
async def test_redelivery_is_acknowledged_and_notified_once(pg, notifications):
    assert (await _receive(pg))["status"] == "accepted"
    assert (await _receive(pg))["status"] == "duplicate"
    assert notifications[0].await_count == 0
    # No in-memory task needed: another session discovers committed queue rows.
    await worker.process_pending_refunds()
    await worker.process_pending_refunds()
    for send in notifications:
        send.assert_awaited_once()
    async with pg() as db:
        assert (await db.get(Order, "ord_test")).status == OrderStatus.REFUNDED
        rows = list(await db.scalars(select(WebhookEvent).order_by(WebhookEvent.id)))
        assert [r.status for r in rows] == [
            WebhookEventStatus.PROCESSED,
            WebhookEventStatus.DUPLICATE,
        ]
        assert rows[0].order_id == "ord_test"


@pytest.mark.postgres
async def test_legacy_success_without_event_id_prevents_new_notification(pg, notifications):
    async with pg() as db:
        db.add(
            WebhookEvent(
                provider=WebhookProvider.RAZORPAY,
                event_type="refund.processed",
                event_id=None,
                payload=_payload(),
                signature_valid=True,
                status=WebhookEventStatus.PROCESSED,
            )
        )
        await db.commit()
    await _receive(pg)
    await worker.process_pending_refunds()
    for send in notifications:
        send.assert_not_awaited()
    async with pg() as db:
        row = await db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == "evt_test"))
        assert row.status == WebhookEventStatus.DUPLICATE


@pytest.mark.postgres
@pytest.mark.parametrize("event_ids", [("evt_a", "evt_b"), (None, None)])
async def test_concurrent_deliveries_with_different_or_missing_ids_notify_once(
    pg, notifications, event_ids
):
    await asyncio.gather(*(_receive(pg, event_id=i) for i in event_ids))
    async with pg() as db:
        ids = list(await db.scalars(select(WebhookEvent.id)))
    await asyncio.gather(*(worker._process_refund(i) for i in ids))
    for send in notifications:
        send.assert_awaited_once()
    async with pg() as db:
        statuses = list(await db.scalars(select(WebhookEvent.status)))
        assert statuses.count(WebhookEventStatus.PROCESSED) == 1
        assert statuses.count(WebhookEventStatus.DUPLICATE) == 1


@pytest.mark.postgres
async def test_same_event_id_race_inserts_one_queue_row(pg, notifications):
    results = await asyncio.gather(_receive(pg), _receive(pg))
    assert sorted(r["status"] for r in results) == ["accepted", "duplicate"]
    await asyncio.gather(worker.process_pending_refunds(), worker.process_pending_refunds())
    for send in notifications:
        send.assert_awaited_once()


@pytest.mark.postgres
async def test_created_and_processed_are_separate_lifecycle_notifications(pg, notifications):
    await _receive(pg, event="refund.created", event_id="evt_created")
    await _receive(pg)
    await worker.process_pending_refunds()
    assert notifications[0].await_count == 2
    notifications[1].assert_awaited_once()
    notifications[2].assert_awaited_once()


@pytest.mark.postgres
async def test_failed_refund_notifies_founder_once(pg, notifications):
    await _receive(pg, event="refund.failed")
    await _receive(pg, event="refund.failed")
    await worker.process_pending_refunds()
    notifications[0].assert_not_awaited()
    notifications[1].assert_awaited_once()
    notifications[2].assert_not_awaited()
    async with pg() as db:
        assert (await db.get(Order, "ord_test")).status == OrderStatus.REFUND_FAILED


@pytest.mark.postgres
async def test_distinct_partial_refunds_are_not_suppressed(pg, notifications):
    await _receive(pg)
    await _receive(pg, event_id="evt_second", refund_id="rfnd_second")
    await worker.process_pending_refunds()
    for send in notifications:
        assert send.await_count == 2


@pytest.mark.postgres
async def test_handler_failure_is_preserved_without_blind_notification_retry(pg, monkeypatch):
    failing = AsyncMock(side_effect=RuntimeError("notification failed"))
    monkeypatch.setitem(worker.REFUND_HANDLERS, "refund.processed", failing)
    await _receive(pg)
    await worker.process_pending_refunds()
    await worker.process_pending_refunds()
    failing.assert_awaited_once()
    async with pg() as db:
        row = await db.scalar(select(WebhookEvent))
        assert row.status == WebhookEventStatus.FAILED
        assert row.error_message == "notification failed"


@pytest.mark.postgres
async def test_unrelated_constraint_failure_is_not_acknowledged_as_duplicate(pg, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    async with pg() as db:
        monkeypatch.setattr(
            db, "flush", AsyncMock(side_effect=IntegrityError("test", {}, Exception()))
        )
        with pytest.raises(IntegrityError):
            await razorpay.razorpay_webhook(_request(_payload()), db)
