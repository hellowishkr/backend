"""Retry stuck/failed top-up payment-link creations and cancellations.

The async multi-phase flow in app/api/admin_orders.py
(create_topup_link -> _advance_creating_link) fires a fast in-request
background attempt, but that attempt is best-effort — a process restart or
worker recycle mid-flight loses it silently. This job is the actual
correctness guarantee, handling four recovery cases:

  - status='creating' rows older than _STUCK_GRACE_SECONDS: the insert
    committed but _advance_creating_link's phase-1 claim never ran (or is
    still legitimately in flight — the grace period avoids racing a call
    that's merely slow).
  - status='claiming' rows older than _STUCK_GRACE_SECONDS: phase 1 (claim)
    committed but the process died before phase 3 (persist) — reset to
    'creating' so _advance_creating_link's claim can run again.
  - status='failed' rows below _MAX_RETRIES: create_payment_link raised;
    retry with the same backoff shape as retry_failed_outbound — but ONLY
    after re-confirming the order still has an outstanding balance (a newer
    top-up link or an order edit may have already settled it; retrying a
    stale 'failed' row unconditionally would re-request payment for money
    already collected).
  - status='cancel_pending' rows older than _STUCK_GRACE_SECONDS: our cancel
    call never completed (or never ran) — retry the Razorpay cancel.
"""

import logging
import time as _time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.db.session import async_session
from app.models.order import Order
from app.models.order_payment_link import OrderPaymentLink
from app.services.razorpay_service import razorpay_service
from app.utils.alerts import send_founder_alert
from app.utils.db_retry import is_transient_db_error, retry_db_operation

logger = logging.getLogger("wishkr.topup_link_reconciliation")

_MAX_RETRIES = 3
_STUCK_GRACE_SECONDS = 60
_DLQ_ALERT_THRESHOLD = 5
_DLQ_ALERT_INTERVAL_SECONDS = 3600
_last_dlq_alert_ts: float = 0.0


def _is_retry_due(row: OrderPaymentLink, now: datetime) -> bool:
    """Simple retry backoff, mirroring retry_failed_outbound._is_retry_due.

    Measured from state_changed_at, not created_at — a row that's been
    'created' for an hour and just transitioned to 'cancel_pending' has an
    old created_at but has been in ITS CURRENT state for zero seconds; using
    created_at would make it look immediately overdue for every retry tier.
    """
    changed_at = row.state_changed_at or row.created_at
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    age = now - changed_at
    retry_count = row.retry_count or 0
    if retry_count == 0:
        return age > timedelta(seconds=_STUCK_GRACE_SECONDS)
    if retry_count == 1:
        return age > timedelta(seconds=30)
    if retry_count == 2:
        return age > timedelta(seconds=60)
    return False


async def _load_stuck_creating(cutoff: datetime) -> list[OrderPaymentLink]:
    async def _do_load() -> list[OrderPaymentLink]:
        async with async_session() as db:
            result = await db.execute(
                select(OrderPaymentLink)
                .where(
                    OrderPaymentLink.status == "creating",
                    OrderPaymentLink.state_changed_at <= cutoff,
                )
                .order_by(OrderPaymentLink.state_changed_at.asc())
            )
            return list(result.scalars().all())

    return await retry_db_operation(_do_load, label="topup_reconcile_load_creating")


async def _load_stuck_claiming(cutoff: datetime) -> list[OrderPaymentLink]:
    async def _do_load() -> list[OrderPaymentLink]:
        async with async_session() as db:
            result = await db.execute(
                select(OrderPaymentLink)
                .where(
                    OrderPaymentLink.status == "claiming",
                    OrderPaymentLink.state_changed_at <= cutoff,
                )
                .order_by(OrderPaymentLink.state_changed_at.asc())
            )
            return list(result.scalars().all())

    return await retry_db_operation(_do_load, label="topup_reconcile_load_claiming")


async def _load_retryable_failed() -> list[OrderPaymentLink]:
    async def _do_load() -> list[OrderPaymentLink]:
        async with async_session() as db:
            result = await db.execute(
                select(OrderPaymentLink)
                .where(
                    OrderPaymentLink.status == "failed",
                    OrderPaymentLink.retry_count < _MAX_RETRIES,
                )
                .order_by(OrderPaymentLink.state_changed_at.asc())
            )
            return list(result.scalars().all())

    return await retry_db_operation(_do_load, label="topup_reconcile_load_failed")


async def _load_stuck_cancel_pending(cutoff: datetime) -> list[OrderPaymentLink]:
    async def _do_load() -> list[OrderPaymentLink]:
        async with async_session() as db:
            result = await db.execute(
                select(OrderPaymentLink)
                .where(
                    OrderPaymentLink.status == "cancel_pending",
                    OrderPaymentLink.state_changed_at <= cutoff,
                )
                .order_by(OrderPaymentLink.state_changed_at.asc())
            )
            return list(result.scalars().all())

    return await retry_db_operation(_do_load, label="topup_reconcile_load_cancel_pending")


async def _retry_creating_row(link_id: str, now: datetime) -> None:
    # Local import to avoid a routing-module import cycle, matching the
    # local `from app.api.admin_settings import record_audit` convention
    # already used inside admin_orders.py's own endpoints.
    from app.api.admin_orders import _advance_creating_link

    async with async_session() as db:
        row = await db.get(OrderPaymentLink, link_id)
        if row is None or row.status != "creating":
            return
        if not _is_retry_due(row, now):
            return
    await _advance_creating_link(link_id)


async def _retry_claiming_row(link_id: str, now: datetime) -> None:
    """A row stuck in 'claiming' means a previous _advance_creating_link call
    committed phase 1 (the claim) but never reached phase 3 (persist) —
    process crash/restart mid-flight. Reset to 'creating' so a fresh call can
    claim it again; safe because 'claiming' is otherwise unreachable except
    via that same claim UPDATE, so there's no concurrent owner to conflict
    with."""
    from app.api.admin_orders import _advance_creating_link

    async with async_session() as db:
        row = await db.get(OrderPaymentLink, link_id)
        if row is None or row.status != "claiming":
            return
        if not _is_retry_due(row, now):
            return
        row.status = "creating"
        row.state_changed_at = now
        await db.commit()
    await _advance_creating_link(link_id)


async def _retry_failed_row(link_id: str, now: datetime) -> None:
    from app.api.admin_orders import _outstanding_amount_paise, _advance_creating_link

    async with async_session() as db:
        row = await db.get(OrderPaymentLink, link_id)
        if row is None or row.status != "failed":
            return
        if not _is_retry_due(row, now):
            return

        order = await db.get(Order, row.order_id)
        if order is None:
            return

        # Re-check the outstanding balance before resurrecting this row — a
        # newer top-up link or an order edit may have already settled the
        # order since this row failed. Retrying unconditionally would send a
        # second payment request for money already collected.
        outstanding_paise = await _outstanding_amount_paise(db, order)
        if outstanding_paise <= 0:
            row.status = "cancelled"
            row.state_changed_at = now
            row.last_error = "order settled elsewhere before this retry ran"
            await db.commit()
            logger.info(
                "topup_link_reconciliation: order=%s settled before retrying failed link=%s; "
                "marked cancelled instead",
                order.id, link_id,
            )
            return

        row.status = "creating"
        row.state_changed_at = now
        await db.commit()
    await _advance_creating_link(link_id)


async def _retry_cancel_pending_row(link_id: str, now: datetime) -> None:
    async with async_session() as db:
        row = await db.get(OrderPaymentLink, link_id)
        if row is None or row.status != "cancel_pending":
            return
        if not _is_retry_due(row, now):
            return
        razorpay_link_id = row.razorpay_link_id

    if not razorpay_link_id:
        # Nothing was ever created on Razorpay for this row — safe to mark
        # cancelled directly, no external call needed.
        async with async_session() as db:
            claim = await db.execute(
                update(OrderPaymentLink)
                .where(OrderPaymentLink.id == link_id, OrderPaymentLink.status == "cancel_pending")
                .values(status="cancelled", state_changed_at=datetime.now(timezone.utc))
            )
            await db.commit()
        return

    try:
        await razorpay_service.cancel_payment_link(razorpay_link_id)
        result_status, result_error = "cancelled", None
    except Exception as e:  # noqa: BLE001 — retried again next tick
        result_status, result_error = None, str(e)[:500]
        logger.warning(
            "topup_link_reconciliation cancel retry failed link=%s: %s", link_id, e,
        )

    async with async_session() as db:
        row = await db.get(OrderPaymentLink, link_id)
        if row is None or row.status != "cancel_pending":
            return
        if result_status == "cancelled":
            row.status = "cancelled"
        else:
            row.retry_count = (row.retry_count or 0) + 1
            row.last_error = result_error
        row.state_changed_at = datetime.now(timezone.utc)
        await db.commit()


async def _count_stuck_rows() -> int:
    async def _do_count() -> int:
        async with async_session() as db:
            result = await db.execute(
                select(func.count(OrderPaymentLink.id)).where(
                    OrderPaymentLink.status == "failed",
                    OrderPaymentLink.retry_count >= _MAX_RETRIES,
                )
            )
            return result.scalar_one()

    return await retry_db_operation(_do_count, label="topup_reconcile_dlq_count")


def _log_row_error(kind: str, row_id: str, e: Exception) -> None:
    if is_transient_db_error(e):
        logger.warning(
            "topup_link_reconciliation %s row processing skipped (transient) id=%s: %s",
            kind, row_id, e,
        )
    else:
        logger.exception(
            "topup_link_reconciliation %s row processing failed id=%s", kind, row_id
        )


async def topup_link_reconciliation() -> None:
    """Reconcile stuck 'creating'/'claiming'/'cancel_pending' rows and retry
    'failed' ones.

    Fresh session per row (a DB blip mid-batch doesn't lose progress). Never
    raises for a single row's Razorpay failure — increments retry_count and
    lets the next tick pick it up, same pattern as retry_failed_outbound.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_STUCK_GRACE_SECONDS)

    try:
        creating_rows = await _load_stuck_creating(cutoff)
        claiming_rows = await _load_stuck_claiming(cutoff)
        failed_rows = await _load_retryable_failed()
        cancel_pending_rows = await _load_stuck_cancel_pending(cutoff)
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                "topup_link_reconciliation skipped: DB unavailable (transient): %s", e
            )
            return
        logger.exception("topup_link_reconciliation failed to load rows")
        raise

    for row in creating_rows:
        try:
            await _retry_creating_row(row.id, now)
        except Exception as e:
            _log_row_error("creating", row.id, e)

    for row in claiming_rows:
        try:
            await _retry_claiming_row(row.id, now)
        except Exception as e:
            _log_row_error("claiming", row.id, e)

    for row in failed_rows:
        try:
            await _retry_failed_row(row.id, now)
        except Exception as e:
            _log_row_error("failed", row.id, e)

    for row in cancel_pending_rows:
        try:
            await _retry_cancel_pending_row(row.id, now)
        except Exception as e:
            _log_row_error("cancel_pending", row.id, e)

    try:
        stuck_count = await _count_stuck_rows()
    except Exception as e:
        logger.warning("topup_link_reconciliation DLQ count failed: %s", e)
        return

    if stuck_count and stuck_count >= _DLQ_ALERT_THRESHOLD:
        global _last_dlq_alert_ts
        if _time.time() - _last_dlq_alert_ts >= _DLQ_ALERT_INTERVAL_SECONDS:
            _last_dlq_alert_ts = _time.time()
            try:
                await send_founder_alert(
                    f"⚠️ Top-up payment link DLQ: {stuck_count} link(s) exhausted "
                    f"all retries and are stuck 'failed'. Inspect order_payment_links "
                    f"and retry manually or notify the affected admin."
                )
            except Exception:
                logger.exception("Failed to send topup link DLQ alert")
