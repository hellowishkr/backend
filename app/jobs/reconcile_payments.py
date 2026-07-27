"""Reconciliation cron: detect 'money in, no voucher out' silent failures.

The Razorpay payment-link webhook is the only path that issues a Hubble
voucher. If the webhook is permanently lost (Razorpay disabled webhooks,
our outage longer than their retry window, signature-misconfig drop), the
user is charged and never receives a voucher — and we have no idea unless
they complain.

This job runs hourly and surfaces:
  * orders with ``razorpay_payment_id`` set but ``status`` not in a
    terminal-fulfilled state, AND any non-deferred Hubble purchases lack
    ``hubble_order_id``.

It only alerts. It does NOT auto-issue; Razorpay webhook redelivery or a
founder-triggered manual replay resumes the idempotent
``handle_payment_link_paid`` path.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import async_session
from app.models import Order, OrderStatus, Purchase
from app.services.hubble_fulfillment import should_defer_hubble_issue
from app.utils.alerts import send_founder_alert

logger = logging.getLogger("wishkr.jobs.reconcile_payments")


# Allow normal handler latency (Hubble retries can take ~1 min) before flagging.
_RECONCILE_GRACE_MINUTES = 10
# Don't keep alerting on the same backlog forever — the founder fixes them
# manually. We only look at orders paid in the last 48 h.
_RECONCILE_LOOKBACK_HOURS = 48
_BATCH_LIMIT = 100


async def reconcile_payments() -> None:
    now = datetime.now(timezone.utc)
    grace_cutoff = now - timedelta(minutes=_RECONCILE_GRACE_MINUTES)
    lookback_cutoff = now - timedelta(hours=_RECONCILE_LOOKBACK_HOURS)

    async with async_session() as db:
        result = await db.execute(
            select(Order)
            .where(
                Order.razorpay_payment_id.is_not(None),
                # NEEDS_MANUAL_REVIEW is included so a partial-failure order
                # whose immediate founder alert was missed (Meta 5xx, founder
                # phone misconfigured) still resurfaces here on the next tick.
                Order.status.in_((
                    OrderStatus.PROCESSING,
                    OrderStatus.PENDING,
                    OrderStatus.PAID,
                    OrderStatus.NEEDS_MANUAL_REVIEW,
                )),
                Order.updated_at < grace_cutoff,
                Order.updated_at >= lookback_cutoff,
            )
            .limit(_BATCH_LIMIT)
        )
        suspect_orders = list(result.scalars().all())

        if not suspect_orders:
            return

        flagged: list[str] = []
        for order in suspect_orders:
            p_result = await db.execute(
                select(Purchase).where(Purchase.order_id == order.id)
            )
            purchases = list(p_result.scalars().all())
            missing = [
                p
                for p in purchases
                if (
                    not p.hubble_order_id
                    and p.hubble_product_id
                    and not should_defer_hubble_issue(p, now=now)
                )
            ]
            if missing:
                flagged.append(
                    f"order={order.id} status={order.status.value} "
                    f"payment={order.razorpay_payment_id} "
                    f"missing_purchases={[p.id for p in missing]}"
                )

        if flagged:
            logger.error(
                "Reconciliation: %d orders paid but no Hubble voucher",
                len(flagged),
            )
            try:
                await send_founder_alert(
                    "🔴 PAYMENT/VOUCHER RECONCILIATION ALERT\n"
                    f"{len(flagged)} order(s) charged on Razorpay but no "
                    f"Hubble voucher issued (within last {_RECONCILE_LOOKBACK_HOURS}h, "
                    f"older than {_RECONCILE_GRACE_MINUTES}min):\n\n"
                    + "\n".join(flagged[:10])
                    + ("\n…(truncated)" if len(flagged) > 10 else "")
                )
            except Exception:
                logger.exception("Failed to send reconciliation alert")
