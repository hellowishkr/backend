"""Razorpay webhook dispatcher.

Responsibilities:
  1. Verify signature (fail closed in production)
  2. Persist every event to webhook_events (immutable audit log — legal paper trail)
  3. Dedupe by Razorpay event_id
  4. Dispatch to the per-event handler in razorpay_handlers.py
  5. Record processing outcome on the audit row

Handlers must NOT raise — exceptions bubble up here and are caught so we
never return 5xx to Razorpay (which would trigger retries and pile up events).
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models import WebhookEvent, WebhookProvider, WebhookEventStatus, DisputeStatus
from app.services.razorpay_service import razorpay_service
from app.webhooks import razorpay_handlers as h

router = APIRouter()
logger = logging.getLogger("wishkr.webhooks.razorpay")


# event_type -> (handler, extracts order_id from payload)
def _order_id_from_payment_link(payload: dict) -> str | None:
    reference_id = payload.get("payload", {}).get("payment_link", {}).get("entity", {}).get("reference_id")
    # Top-up links use reference_id="opl_..." (an OrderPaymentLink id, not an
    # Order id) — routed internally to handle_topup_payment_link_paid, which
    # resolves and returns the real order_id in its result. Returning None
    # here (like the dispute extractor does) lets the post-handler fix-up
    # below fill in the correct order_id instead of the audit log
    # permanently recording the opl_ reference as if it were an order id.
    if isinstance(reference_id, str) and reference_id.startswith("opl_"):
        return None
    return reference_id


def _order_id_from_payment(payload: dict) -> str | None:
    pay = payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}
    return pay.get("order_id") or (pay.get("notes") or {}).get("order_id")


def _order_id_from_dispute(payload: dict) -> str | None:
    # Order is resolved inside the handler via payment_id lookup; return None here.
    return None


def _no_order(_payload: dict) -> str | None:
    # For events with no associated order (downtime, settlement)
    return None


EVENT_HANDLERS = {
    "payment_link.paid":              (h.handle_payment_link_paid,              _order_id_from_payment_link),
    "payment_link.partially_paid":    (h.handle_payment_link_partially_paid,    _order_id_from_payment_link),
    "payment_link.expired":           (h.handle_payment_link_expired,           _order_id_from_payment_link),
    "payment_link.cancelled":         (h.handle_payment_link_cancelled,         _order_id_from_payment_link),
    "payment.failed":                 (h.handle_payment_failed,                 _order_id_from_payment),
    "payment.captured":               (h.handle_payment_captured,               _order_id_from_payment),
    "payment.dispute.created":        (h.handle_dispute_created,                _order_id_from_dispute),
    "payment.dispute.won":            (h.handle_dispute_won,                    _order_id_from_dispute),
    "payment.dispute.lost":           (h.handle_dispute_lost,                   _order_id_from_dispute),
    "payment.downtime.started":       (h.handle_payment_downtime_started,       _no_order),
    "payment.downtime.updated":       (h.handle_payment_downtime_updated,       _no_order),
    "payment.downtime.resolved":      (h.handle_payment_downtime_resolved,      _no_order),
    "refund.created":                 (h.handle_refund_created,                 _order_id_from_dispute),
    "refund.processed":               (h.handle_refund_processed,               _order_id_from_dispute),
    "refund.failed":                  (h.handle_refund_failed,                  _order_id_from_dispute),
    "settlement.processed":           (h.handle_settlement_processed,           _no_order),
}

# Dispute status-change events share one handler with different target statuses
DISPUTE_STATUS_EVENTS = {
    "payment.dispute.under_review":   DisputeStatus.UNDER_REVIEW,
    "payment.dispute.action_required": DisputeStatus.ACTION_REQUIRED,
    "payment.dispute.closed":         DisputeStatus.CLOSED,
}


@router.post("/razorpay")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    signature_valid: bool | None = None
    if not settings.razorpay_webhook_secret:
        # Fail-closed in production: a missing secret indicates a misconfigured
        # deploy. Dev stays fail-open so local testing without secrets works.
        if (settings.app_env or "development").lower() == "production":
            logger.error("RAZORPAY_WEBHOOK_SECRET missing in production — rejecting webhook")
            raise HTTPException(status_code=503, detail="webhook secret not configured")
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping signature check (dev only)")
    else:
        signature_valid = razorpay_service.verify_webhook_signature(body, signature)
        if not signature_valid:
            # Persist invalid signature attempts too — they are valuable forensics
            try:
                invalid_payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                invalid_payload = {"raw": body.decode("utf-8", errors="replace")[:2000]}
            db.add(WebhookEvent(
                provider=WebhookProvider.RAZORPAY,
                event_type=invalid_payload.get("event", "unknown"),
                event_id=invalid_payload.get("id"),
                payload=invalid_payload,
                signature=signature[:255],
                signature_valid=False,
                status=WebhookEventStatus.FAILED,
                error_message="Invalid signature",
            ))
            await db.commit()
            raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Razorpay webhook: invalid JSON")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event", "")
    event_id = payload.get("id")
    logger.info("Razorpay webhook: event=%s id=%s", event_type, event_id)

    # Resolve handler
    handler = None
    order_id_fn = None
    if event_type in EVENT_HANDLERS:
        handler, order_id_fn = EVENT_HANDLERS[event_type]
    elif event_type in DISPUTE_STATUS_EVENTS:
        new_status = DISPUTE_STATUS_EVENTS[event_type]
        async def _dispatch(p, d, ns=new_status):
            return await h.handle_dispute_status_update(p, d, ns)
        handler = _dispatch
        order_id_fn = _order_id_from_dispute

    order_id_guess = order_id_fn(payload) if order_id_fn else None

    # Atomic dedup: rely on UniqueConstraint(provider, event_id) instead of a
    # racy SELECT-then-INSERT. A second concurrent delivery of the same event
    # raises IntegrityError on flush — we roll back, log a DUPLICATE row in a
    # fresh transaction and return without invoking the handler.
    audit = WebhookEvent(
        provider=WebhookProvider.RAZORPAY,
        event_type=event_type,
        event_id=event_id,
        payload=payload,
        signature=signature[:255] if signature else None,
        signature_valid=signature_valid,
        status=WebhookEventStatus.RECEIVED,
        order_id=order_id_guess,
    )
    db.add(audit)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        if event_id:
            db.add(WebhookEvent(
                provider=WebhookProvider.RAZORPAY,
                event_type=event_type,
                event_id=None,  # avoid colliding with the unique constraint again
                payload=payload,
                signature=signature[:255] if signature else None,
                signature_valid=signature_valid,
                status=WebhookEventStatus.DUPLICATE,
                error_message=f"Duplicate of provider event_id={event_id}",
            ))
            await db.commit()
        return {"status": "duplicate", "event_id": event_id}

    if not handler:
        audit.status = WebhookEventStatus.IGNORED
        audit.processed_at = datetime.now(timezone.utc)
        db.add(audit)
        await db.commit()
        return {"status": "ignored", "event": event_type}

    try:
        result = await handler(payload, db)
        audit.status = WebhookEventStatus.PROCESSED
        audit.processed_at = datetime.now(timezone.utc)
        # If handler resolved an order_id (e.g. dispute → order via payment_id), record it
        if not audit.order_id and isinstance(result, dict) and result.get("order_id"):
            audit.order_id = result["order_id"]
        db.add(audit)
        await db.commit()
        return {"status": "ok", "event": event_type, "result": result}

    except Exception as e:
        logger.exception("Handler failed for event=%s: %s", event_type, e)
        # Don't abort — record failure on audit row so we don't lose the event
        await db.rollback()
        db.add(WebhookEvent(
            provider=WebhookProvider.RAZORPAY,
            event_type=event_type,
            event_id=event_id,
            payload=payload,
            signature=signature[:255] if signature else None,
            signature_valid=signature_valid,
            status=WebhookEventStatus.FAILED,
            error_message=str(e)[:1000],
            order_id=order_id_guess,
            processed_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        # Return 200 so Razorpay doesn't retry forever — we've persisted the event
        return {"status": "error_logged", "event": event_type}
