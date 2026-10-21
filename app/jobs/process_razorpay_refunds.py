"""Drain refund webhooks saved before acknowledging Razorpay's 5-second deadline."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import async_session
from app.models import Order, WebhookEvent, WebhookEventStatus, WebhookProvider
from app.webhooks import razorpay_handlers as handlers

logger = logging.getLogger("wishkr.jobs.process_razorpay_refunds")

REFUND_HANDLERS = {
    "refund.created": handlers.handle_refund_created,
    "refund.processed": handlers.handle_refund_processed,
    "refund.failed": handlers.handle_refund_failed,
}


async def _process_refund(audit_id: int) -> None:
    async with async_session() as db:
        audit = await db.scalar(
            select(WebhookEvent)
            .where(
                WebhookEvent.id == audit_id,
                WebhookEvent.provider == WebhookProvider.RAZORPAY,
                WebhookEvent.status == WebhookEventStatus.RECEIVED,
                WebhookEvent.event_type.in_(REFUND_HANDLERS),
                WebhookEvent.signature_valid.is_not(False),
            )
            .with_for_update(skip_locked=True)
        )
        if audit is None:
            return

        refund = audit.payload.get("payload", {}).get("refund", {}).get("entity", {})
        # Serialize refunds for the same order, including separate deliveries
        # with different/missing event IDs. Refund handlers do not commit.
        await db.scalar(
            select(Order.id)
            .where(Order.razorpay_payment_id == refund.get("payment_id"))
            .with_for_update()
        )
        previous = None
        if refund.get("id"):
            previous = await db.scalar(
                select(WebhookEvent.id)
                .where(
                    WebhookEvent.provider == WebhookProvider.RAZORPAY,
                    WebhookEvent.event_type == audit.event_type,
                    WebhookEvent.status == WebhookEventStatus.PROCESSED,
                    WebhookEvent.signature_valid.is_not(False),
                    WebhookEvent.payload["payload"]["refund"]["entity"]["id"].astext
                    == refund["id"],
                )
                .limit(1)
            )
        if previous is not None:
            audit.status = WebhookEventStatus.DUPLICATE
            audit.error_message = f"Refund lifecycle event already processed by audit_id={previous}"
        else:
            try:
                # A savepoint preserves the queue-row lock if a handler fails.
                async with db.begin_nested():
                    result = await REFUND_HANDLERS[audit.event_type](audit.payload, db)
                audit.status = WebhookEventStatus.PROCESSED
                audit.order_id = result.get("order_id") or audit.order_id
            except Exception as exc:
                logger.exception("Refund handler failed audit_id=%s", audit_id)
                # Do not blindly retry possibly-sent external notifications.
                # Preserve the failed event for operator review.
                audit.status = WebhookEventStatus.FAILED
                audit.error_message = str(exc)[:1000]
        audit.processed_at = datetime.now(timezone.utc)
        await db.commit()


async def process_pending_refunds() -> None:
    async with async_session() as db:
        ids = list(
            await db.scalars(
                select(WebhookEvent.id)
                .where(
                    WebhookEvent.provider == WebhookProvider.RAZORPAY,
                    WebhookEvent.status == WebhookEventStatus.RECEIVED,
                    WebhookEvent.event_type.in_(REFUND_HANDLERS),
                    WebhookEvent.signature_valid.is_not(False),
                )
                .order_by(WebhookEvent.id)
                .limit(20)
            )
        )
    for audit_id in ids:
        await _process_refund(audit_id)
