"""Cron job: find stale orders and handle them."""

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.db.session import async_session
from app.models import Order, OrderStatus
from app.utils.alerts import send_founder_alert

logger = logging.getLogger("wishkr.jobs.stale_orders")


# Razorpay payment links are minted with a 30-min ``expire_by``. We must wait
# strictly *longer* than that before declaring the local Order EXPIRED, or a
# user who pays at minute 28 lands in a state where the link is paid but the
# Order is already CANCELLED/EXPIRED — ``handle_payment_link_paid`` then
# returns ``invalid_state`` and silently drops the money. 35 min gives 5 min
# slack for clock skew + webhook latency.
_STALE_ORDER_EXPIRY_MINUTES = 35
_STALE_ORDER_BATCH_LIMIT = 200


async def poll_stale_orders():
    """Expire abandoned PENDING orders + detect paid-after-expiry races.

    The legacy "PAID orders stuck without voucher" branch was removed: the
    handler never sets ``OrderStatus.PAID`` (PENDING → PROCESSING → COMPLETED),
    and the legacy code passed ``order.user_id`` (a ``usr_xxx`` id, not a
    phone) to Hubble + never sent the voucher to the user — so on the rare
    real fire it would have triple-billed. Reconciliation lives in the
    dedicated ``reconcile_payments`` job instead.
    """
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        cutoff_pending = now - timedelta(minutes=_STALE_ORDER_EXPIRY_MINUTES)

        result = await db.execute(
            select(Order)
            .where(
                Order.status == OrderStatus.PENDING,
                Order.created_at < cutoff_pending,
            )
            .limit(_STALE_ORDER_BATCH_LIMIT)
        )
        expired_orders = result.scalars().all()

        # Per-row commit: a transient DB error after row N would otherwise
        # roll back rows 0..N-1 and we'd have to re-detect the same set on
        # the next tick (and re-alert on every paid-after-expiry).
        for order in expired_orders:
            try:
                if order.razorpay_payment_id:
                    # Paid-after-expiry detection: Razorpay set payment_id on
                    # the Order before the webhook flipped status (lost
                    # webhook / late arrival). Don't silently downgrade —
                    # alert so the voucher can be issued or the user refunded.
                    logger.error(
                        "Stale order %s has razorpay_payment_id=%s — paid-after-expiry race",
                        order.id, order.razorpay_payment_id,
                    )
                    try:
                        await send_founder_alert(
                            f"⚠️ Order {order.id} crossed expiry but already has "
                            f"razorpay_payment_id={order.razorpay_payment_id}. "
                            f"User may have paid; voucher NOT issued. Manual review needed."
                        )
                    except Exception:
                        pass
                    order.status = OrderStatus.NEEDS_MANUAL_REVIEW
                else:
                    order.status = OrderStatus.EXPIRED
                    logger.info(
                        "Order %s expired (no payment after %d min)",
                        order.id, _STALE_ORDER_EXPIRY_MINUTES,
                    )
                db.add(order)
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.exception("poll_stale_orders: commit failed for %s: %s", order.id, e)
