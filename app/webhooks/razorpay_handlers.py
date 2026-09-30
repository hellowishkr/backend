"""Individual handlers for each Razorpay webhook event.

Each handler receives a dict `payload` (the full webhook body) and a `db` session.
Handlers are responsible for:
  - Updating the order/purchase state
  - Notifying the user via WhatsApp
  - Alerting the founder for critical events
  - Returning a structured response dict

Handlers MUST NOT raise — catch and log. The dispatcher records any failure in
webhook_events.status = FAILED with error_message populated.
"""

import html as html_lib
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Order, OrderStatus, OrderLineItem, OrderPaymentLink, Purchase, PurchaseStatus, DeliveryStatus, User,
    Vendor, Product, ProductVariant,
    Dispute, DisputeStatus, WebhookEvent,
)
from app.services.hubble import HubbleOrderError
from app.services.hubble_fulfillment import issue_hubble_voucher, should_defer_hubble_issue
from app.services.razorpay_service import razorpay_service
from app.services.whatsapp import whatsapp_service, is_in_cs_window, send_text_or_template
from app.core.config import settings
from app.bot import messages as msg
from app.bot.whatsapp_templates import (
    TEMPLATE_ACCOUNT_NOTIFICATION,
    TEMPLATE_ORDER_CONFIRMATION,
    TEMPLATE_ORDER_STATUS_UPDATE,
    TEMPLATE_PAYMENT_FAILED,
    TEMPLATE_REFUND_UPDATE,
    TEMPLATE_VENDOR_ORDER_NOTIFICATION,
    _first_name,
    order_confirmation_params,
    payment_failed_params,
    vendor_order_notification_params,
)
from app.utils.alerts import send_founder_alert
from app.utils.email import send_email
from app.utils.observability import mask_phone

logger = logging.getLogger("wishkr.webhooks.razorpay.handlers")


def _entity(payload: dict, key: str) -> dict:
    return payload.get("payload", {}).get(key, {}).get("entity", {}) or {}


async def _get_order(db: AsyncSession, order_id: str) -> Order | None:
    if not order_id:
        return None
    result = await db.execute(select(Order).where(Order.id == order_id))
    return result.scalar_one_or_none()


async def _get_user(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _auto_refund_on_total_failure(order: Order, user: User | None) -> None:
    """Issue a Razorpay refund for an order whose every Hubble purchase failed.

    Conservative policy:
      * Only fires when ALL purchases on the order failed at Hubble.
      * Partial successes (some vouchers issued, some failed) intentionally
        skip auto-refund — a full refund would claw back money against an
        already-issued voucher, and a partial refund needs operator
        judgement on which line to refund.
      * Any failure of the refund call itself is alerted; the founder will
        process the refund manually. We do NOT mutate order status here —
        ``refund.created`` / ``refund.processed`` webhooks drive the
        lifecycle forward.
    """
    if not order.razorpay_payment_id:
        return
    try:
        amount_paise = int(order.total_amount * 100)
        # payment_link.paid can fire while the payment is still in `authorized`
        # state. Razorpay only allows refunds on captured payments, so capture
        # first. Ignore errors — if already captured this is a no-op.
        try:
            payment = await razorpay_service.fetch_payment(order.razorpay_payment_id)
            if payment.get("status") == "authorized":
                await razorpay_service.capture_payment(order.razorpay_payment_id, amount_paise)
        except Exception as cap_err:
            logger.warning("Capture attempt before refund failed (may already be captured): %s", cap_err)

        result = await razorpay_service.refund_payment(order.razorpay_payment_id, amount_paise=amount_paise)
        refund_id = (result or {}).get("id")
        logger.info(
            "Auto-refund initiated for order=%s payment=%s refund=%s",
            order.id, order.razorpay_payment_id, refund_id,
        )
        if user:
            try:
                await send_text_or_template(
                    user.phone,
                    msg.REFUND_INITIATED.format(
                        order_id=order.id, amount=int(order.total_amount),
                    ),
                    last_inbound_at=user.last_inbound_at,
                    template_name=TEMPLATE_REFUND_UPDATE,
                    body_params=[
                        _first_name(user.name),
                        str(int(order.total_amount)),
                        order.id,
                        "initiated",
                    ],
                )
            except Exception as e:
                logger.error(
                    "Auto-refund: failed to notify user phone=%s err=%s",
                    mask_phone(user.phone), e,
                )
    except Exception as e:
        # Refund call failed — surface immediately. The order is already
        # NEEDS_MANUAL_REVIEW so founder will see it; this alert adds the
        # specific "tried to auto-refund but couldn't" context.
        logger.exception("Auto-refund failed for order=%s: %s", order.id, e)
        try:
            await send_founder_alert(
                f"⚠️ AUTO-REFUND FAILED for order {order.id} "
                f"(payment {order.razorpay_payment_id}). Manual refund required. "
                f"Error: {str(e)[:200]}"
            )
        except Exception:
            pass


# ─── payment_link.paid ─────────────────────────────────────
async def handle_payment_link_paid(payload: dict, db: AsyncSession) -> dict:
    """Voucher fulfilment after successful payment.

    Transaction strategy (the previous version held a single open txn across
    Hubble's network call and lost the voucher on any rollback):

      1. **Claim** — persist ``razorpay_payment_id`` + ``OrderStatus.PROCESSING``
         and commit immediately. This is the idempotency anchor: a redelivery
         that lost the dedup race at the dispatcher will see ``razorpay_payment_id``
         already set and short-circuit at the guard above.
      2. **Receipt** — for immediate gifts, call Hubble (no DB txn open),
         then write the voucher fields and commit BEFORE attempting to send.
         For future-dated friend gifts, do not call Hubble yet; leave the
         purchase PENDING_DELIVERY so the delivery cron issues the voucher on
         the occasion date. A crash between Hubble-OK and DB-write would leak
         a voucher; we minimise the window by committing immediately after
         the Hubble call returns. ``hubble_order_id`` is the durable receipt
         that the reconciliation cron can use to detect partial failures.
      3. **Delivery** — send WhatsApp. Send failures route to
         ``FailedOutboundMessage`` (handled below) rather than poisoning the
         voucher state.
    """
    payment_link = _entity(payload, "payment_link")
    payment = _entity(payload, "payment")

    payment_id = payment.get("id")
    reference_id = payment_link.get("reference_id")

    if not reference_id:
        return {"status": "no_reference"}

    # Top-up links (post-purchase price-increase payments) use a distinct
    # reference_id prefix ("opl_...", never "ord_...") so they can never be
    # misread as the original order payment — route them to their own handler
    # entirely, which never touches Order.status/razorpay_payment_id.
    if reference_id.startswith("opl_"):
        return await handle_topup_payment_link_paid(payload, db)

    order = await _get_order(db, reference_id)
    if not order:
        return {"status": "order_not_found", "order_id": reference_id}

    if not payment_id:
        return {"status": "no_payment_id", "order_id": order.id}

    if settings.mvp_reminder_only:
        logger.warning(
            "Ignoring Razorpay paid webhook in MVP_REMINDER_ONLY mode: order=%s payment=%s",
            order.id,
            payment_id,
        )
        try:
            await send_founder_alert(
                "⚠️ Razorpay payment_link.paid ignored because MVP_REMINDER_ONLY=true. "
                f"Order: {order.id} | Payment: {payment_id}"
            )
        except Exception:
            logger.exception("Failed to alert founder for MVP ignored payment order=%s", order.id)
        return {
            "status": "ignored_mvp_reminder_only",
            "order_id": order.id,
            "payment_id": payment_id,
        }

    same_payment_retry = order.razorpay_payment_id == payment_id
    if same_payment_retry and order.status == OrderStatus.COMPLETED:
        return {"status": "already_processed", "order_id": order.id}

    if same_payment_retry:
        if order.status not in (
            OrderStatus.PENDING,
            OrderStatus.PAID,
            OrderStatus.PROCESSING,
            OrderStatus.NEEDS_MANUAL_REVIEW,
        ):
            return {
                "status": "already_processed",
                "order_id": order.id,
                "current": order.status.value,
            }
        logger.warning(
            "Resuming Razorpay paid fulfillment for order=%s payment=%s status=%s",
            order.id,
            payment_id,
            order.status.value,
        )
    else:
        if order.razorpay_payment_id:
            return {
                "status": "different_payment_already_recorded",
                "order_id": order.id,
                "current_payment": order.razorpay_payment_id,
            }
        if order.status not in (OrderStatus.PENDING, OrderStatus.PAID):
            return {"status": "invalid_state", "order_id": order.id, "current": order.status.value}

    # ── (1) Claim ──
    if not same_payment_retry:
        order.razorpay_payment_id = payment_id
        order.status = OrderStatus.PROCESSING
        # Freeze what was actually collected at the moment of the ORIGINAL
        # payment. total_amount is live and can be mutated by later
        # post-purchase edits, so this is the only reliable anchor for
        # computing an outstanding balance after an edit raises the total.
        order.amount_collected_paise = int(round(order.total_amount * 100))
        db.add(order)
        await db.commit()  # release txn before the Hubble call

    user = await _get_user(db, order.user_id)

    purchase_result = await db.execute(select(Purchase).where(Purchase.order_id == order.id))
    purchases = purchase_result.scalars().all()

    all_success = True
    deferred_count = 0
    for p in purchases:
        if not p.hubble_product_id:
            continue
        # Defensive: if a Hubble voucher was already issued for this Purchase
        # row (e.g. a webhook retry that slipped past the order-level guard,
        # or a manual replay from the Razorpay dashboard) don't buy a second
        # voucher. The order-level ``razorpay_payment_id`` check at the top
        # of this handler covers same-payment retries; this protects against
        # any other path that lands twice on the same Purchase.
        if p.status in (PurchaseStatus.VOUCHER_ISSUED, PurchaseStatus.DELIVERED):
            logger.info(
                "Skipping already-fulfilled purchase %s (status=%s)",
                p.id, p.status.value,
            )
            continue
        try:
            if should_defer_hubble_issue(p):
                deferred_count += 1
                db.add(p)
                await db.commit()
            else:
                await issue_hubble_voucher(p)
                db.add(p)
                # Commit the durable Hubble receipt before any further work. If
                # the WhatsApp send below fails, the voucher data must not be
                # rolled back — money was spent and we need the receipt for the
                # retry path and reconciliation cron.
                await db.commit()

            if p.buyer_phone and p.occasion_id:
                # Payment confirmed — mark the friend as PURCHASED now that
                # money has actually been received, not when the order was created.
                try:
                    from app.models.celebrant_occasion import CelebrantOccasion
                    from app.models.celebrant_friend import CelebrantFriend, CelebrantFriendStatus
                    cel_occ_res = await db.execute(
                        select(CelebrantOccasion).where(
                            CelebrantOccasion.occasion_id == p.occasion_id
                        )
                    )
                    cel_occ = cel_occ_res.scalar_one_or_none()
                    if cel_occ:
                        cf_res = await db.execute(
                            select(CelebrantFriend).where(
                                CelebrantFriend.celebrant_occasion_id == cel_occ.id,
                                CelebrantFriend.phone == p.buyer_phone,
                            )
                        )
                        cf = cf_res.scalar_one_or_none()
                        if cf and cf.status != CelebrantFriendStatus.PURCHASED:
                            cf.status = CelebrantFriendStatus.PURCHASED
                            db.add(cf)
                            await db.commit()
                except Exception as cf_err:
                    logger.error(
                        "Failed to mark CelebrantFriend PURCHASED for order=%s: %s",
                        order.id, cf_err,
                    )

            if p.delivery_scheduled_at and p.delivery_status == DeliveryStatus.PENDING_DELIVERY:
                if p.buyer_phone:
                    celebrant_name = (user.name if user else None) or "your friend"
                    delivery_date_str = p.delivery_scheduled_at.strftime("%d %B")
                    confirmation_body = msg.FRIEND_PAYMENT_CONFIRMED.format(
                        celebrant_name=celebrant_name,
                        brand=p.brand_name or "Gift Card",
                        amount=int(p.amount),
                        message_preview=f'"{p.personal_message[:50]}..."' if p.personal_message else "No message",
                        delivery_date=delivery_date_str,
                    )
                    # Resolve buyer name once for the template; falls back to
                    # "there" inside ``_first_name`` if no User row exists.
                    buyer_user_res = await db.execute(
                        select(User).where(User.phone == p.buyer_phone)
                    )
                    buyer_user = buyer_user_res.scalar_one_or_none()
                    buyer_name = buyer_user.name if buyer_user else None
                    # Template-vs-text gate, same shape as voucher_delivered
                    # below: the buyer's CS window is usually closed by the
                    # time the webhook fires (they paid via Razorpay, not by
                    # messaging us), so default to the approved Utility
                    # template ``order_confirmation`` outside the window.
                    use_confirm_template = (
                        buyer_user is None
                        or not is_in_cs_window(buyer_user.last_inbound_at)
                    )
                    confirm_params = order_confirmation_params(
                        sender_name=buyer_name,
                        order_id=order.id,
                        brand=p.brand_name or "Gift Card",
                        amount_rupees=p.amount,
                        recipient_label=celebrant_name,
                        delivery_date=delivery_date_str,
                        paid_amount_rupees=p.amount,
                        payment_ref=order.razorpay_payment_id or order.id,
                    )
                    try:
                        if use_confirm_template:
                            await whatsapp_service.send_template(
                                p.buyer_phone,
                                template_name=TEMPLATE_ORDER_CONFIRMATION,
                                body_params=confirm_params,
                            )
                        else:
                            await whatsapp_service.send_text(p.buyer_phone, confirmation_body)
                    except Exception as e:
                        logger.error("Failed to send confirmation to buyer %s: %s", mask_phone(p.buyer_phone), e)
                        # Buyer paid; they MUST get confirmation. Push to durable
                        # retry queue so retry_failed_outbound replays it. Persist
                        # template metadata so the replay reproduces the template
                        # send (a free-text replay outside the CS window would be
                        # silently dropped by Meta).
                        from app.webhooks.whatsapp import _record_failed_outbound
                        await _record_failed_outbound(
                            p.buyer_phone, confirmation_body, "buyer_confirmation", str(e),
                            template_name=TEMPLATE_ORDER_CONFIRMATION if use_confirm_template else None,
                            template_params={"body_params": confirm_params} if use_confirm_template else None,
                        )
            elif user:
                brand = p.brand_name or "Gift Card"
                amount_str = str(int(p.amount))
                code = p.voucher_code or "N/A"
                pin = p.voucher_pin or "N/A"
                expiry_str = (
                    p.voucher_expiry.strftime("%d %b %Y")
                    if p.voucher_expiry else "Check brand T&C"
                )

                voucher_body = msg.VOUCHER_DELIVERED.format(
                    brand=brand,
                    amount=int(p.amount),
                    code=code,
                    pin=pin,
                    expiry=expiry_str,
                )
                # 24h CS window: free-text is allowed only if the user
                # messaged us recently. Otherwise Meta will reject the send
                # and we'd silently fail. The approved utility template
                # `voucher_delivered` matches voucher_body 1:1 in content.
                use_template = (
                    settings.whatsapp_use_voucher_template
                    and not is_in_cs_window(user.last_inbound_at)
                )
                template_body_params = [brand, amount_str, code, pin, expiry_str]
                try:
                    if use_template:
                        await whatsapp_service.send_template(
                            user.phone,
                            "voucher_delivered",
                            body_params=template_body_params,
                        )
                    else:
                        await whatsapp_service.send_text(user.phone, voucher_body)
                    p.status = PurchaseStatus.DELIVERED
                    db.add(p)
                    await db.commit()
                except Exception as e:
                    logger.error("Failed to send voucher to user %s: %s", mask_phone(user.phone), e)
                    # Money taken, voucher in DB, send failed: queue for replay
                    # AND alert the founder so we don't miss this silently.
                    # If we attempted a template send, persist the template
                    # metadata so the retry job replays as a template (Meta
                    # would reject a free-text replay outside the 24h window).
                    from app.webhooks.whatsapp import _record_failed_outbound
                    await _record_failed_outbound(
                        user.phone, voucher_body, "voucher_delivery", str(e),
                        template_name="voucher_delivered" if use_template else None,
                        template_params={"body_params": template_body_params} if use_template else None,
                    )
                    try:
                        await send_founder_alert(
                            f"⚠️ Voucher SEND failed for order {order.id} (phone {user.phone}). "
                            f"Voucher is in DB, queued for retry. Error: {str(e)[:200]}"
                        )
                    except Exception:
                        pass

        except HubbleOrderError as e:
            logger.error("Hubble order failed for purchase %s: %s", p.id, e)
            p.status = PurchaseStatus.FAILED
            db.add(p)
            # Commit the FAILED state so a webhook retry sees the canonical
            # status and the reconciliation cron can pick it up.
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            all_success = False
            if user:
                try:
                    await send_text_or_template(
                        user.phone,
                        msg.VOUCHER_FAILED,
                        last_inbound_at=user.last_inbound_at,
                        template_name=TEMPLATE_ACCOUNT_NOTIFICATION,
                        body_params=[_first_name(user.name), msg.VOUCHER_FAILED],
                    )
                except Exception:
                    pass
                await send_founder_alert(
                    msg.ALERT_FAILED_ORDER.format(
                        order_id=order.id,
                        phone=user.phone,
                        amount=int(p.amount),
                        brand=p.brand_name or "Unknown",
                        error=str(e)[:200],
                    )
                )

    # Decide auto-refund eligibility BEFORE flipping order status — only
    # when EVERY purchase on the order failed at Hubble. Partial successes
    # (some vouchers issued, some failed) intentionally stay manual: a
    # full refund would claw back money the recipient might already have
    # spent against an issued voucher; a partial refund needs operator
    # judgement on which line to refund.
    fully_failed = (
        not all_success
        and order.razorpay_payment_id
        and purchases
        and all(p.status == PurchaseStatus.FAILED for p in purchases if p.hubble_product_id)
    )

    # Even on auto-refund eligibility, status stays NEEDS_MANUAL_REVIEW
    # until ``refund.processed`` arrives and ``handle_refund_processed``
    # flips it to REFUNDED. We don't introduce a new transient enum value
    # because the existing refund webhooks already model the lifecycle.
    order.status = (
        OrderStatus.PAID
        if all_success and deferred_count
        else OrderStatus.COMPLETED if all_success
        else OrderStatus.NEEDS_MANUAL_REVIEW
    )
    db.add(order)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Failed to commit final order status for %s", order.id)

    # Auto-refund happens AFTER the status is committed so a refund-call
    # crash doesn't undo our voucher-failed bookkeeping. The
    # ``refund.created`` and ``refund.processed`` webhooks will drive the
    # final user-visible status change to REFUNDED.
    if fully_failed:
        await _auto_refund_on_total_failure(order, user)

    # Stamp gifted_at on any wishlist items that motivated this order so
    # suggest_gifts stops surfacing them. Only on full success — partial
    # failures stay open so a retry / manual fix can still close the loop.
    if all_success and not deferred_count:
        from app.bot.wishlist_store import mark_gifted
        for p in purchases:
            if p.wishlist_item_id:
                try:
                    await mark_gifted(db, p.wishlist_item_id, order_id=order.id)
                except Exception:
                    logger.exception(
                        "mark_gifted failed wishlist=%s order=%s — order completion not blocked",
                        p.wishlist_item_id, order.id,
                    )

    return {
        "status": "ok",
        "order_id": order.id,
        "all_success": all_success,
        "deferred_count": deferred_count,
    }


async def handle_topup_payment_link_paid(payload: dict, db: AsyncSession) -> dict:
    """Mark a top-up OrderPaymentLink paid. Never touches Order.razorpay_payment_id
    or Order.status — those stay anchored to the original payment. Idempotent
    on OrderPaymentLink.razorpay_payment_id (unique)."""
    payment_link = _entity(payload, "payment_link")
    payment = _entity(payload, "payment")
    link_id = payment_link.get("reference_id")
    payment_id = payment.get("id")

    if not link_id or not payment_id:
        return {"status": "no_reference_or_payment"}

    opl = (
        await db.execute(select(OrderPaymentLink).where(OrderPaymentLink.id == link_id))
    ).scalar_one_or_none()
    if opl is None:
        return {"status": "topup_link_not_found", "link_id": link_id}

    if opl.status == "paid" and opl.razorpay_payment_id == payment_id:
        return {"status": "already_processed", "order_id": opl.order_id}
    if opl.razorpay_payment_id and opl.razorpay_payment_id != payment_id:
        return {"status": "different_payment_already_recorded", "order_id": opl.order_id}

    opl.status = "paid"
    opl.razorpay_payment_id = payment_id
    opl.paid_at = datetime.now(timezone.utc)
    db.add(opl)
    await db.commit()

    logger.info(
        "Top-up link %s paid for order=%s payment=%s", opl.id, opl.order_id, payment_id
    )
    return {"status": "ok", "order_id": opl.order_id, "topup_link_id": opl.id}


async def handle_topup_payment_link_expired(payload: dict, db: AsyncSession) -> dict:
    payment_link = _entity(payload, "payment_link")
    link_id = payment_link.get("reference_id")
    opl = (
        await db.execute(select(OrderPaymentLink).where(OrderPaymentLink.id == link_id))
    ).scalar_one_or_none()
    if opl is None:
        return {"status": "topup_link_not_found", "link_id": link_id}
    # cancel_pending is accepted too — the webhook confirming the terminal
    # state may arrive before our own cancel_pending -> cancelled commit
    # lands; without this, a link stuck in cancel_pending due to a timing
    # race would never resolve, even though Razorpay-side is already terminal.
    if opl.status in ("created", "cancel_pending"):
        opl.status = "expired"
        await db.commit()
    return {"status": "ok", "order_id": opl.order_id, "topup_link_id": opl.id}


async def handle_topup_payment_link_cancelled(payload: dict, db: AsyncSession) -> dict:
    payment_link = _entity(payload, "payment_link")
    link_id = payment_link.get("reference_id")
    opl = (
        await db.execute(select(OrderPaymentLink).where(OrderPaymentLink.id == link_id))
    ).scalar_one_or_none()
    if opl is None:
        return {"status": "topup_link_not_found", "link_id": link_id}
    # cancel_pending is accepted too — see handle_topup_payment_link_expired
    # for why (our own cancel_pending -> cancelled commit may not have
    # landed yet when this webhook arrives).
    if opl.status in ("created", "cancel_pending"):
        opl.status = "cancelled"
        await db.commit()
    return {"status": "ok", "order_id": opl.order_id, "topup_link_id": opl.id}


def _shipping_address_summary(order: Order) -> str:
    parts = [
        order.shipping_name,
        order.shipping_line1,
        order.shipping_line2,
        order.shipping_city,
        order.shipping_state,
        order.shipping_pincode,
    ]
    return ", ".join(p for p in parts if p)


def _looks_like_url(value: str) -> bool:
    return value.strip().startswith(("http://", "https://"))


def _render_line_descriptor(li, products: dict, variants: dict) -> str:
    """One-line descriptor for a line item: name (variant) + add-ons [customizations].

    Shared by the WhatsApp summary and the email body so both show identical,
    complete fulfilment detail. Uploaded files surface as their URL inline.
    """
    product = products.get(li.product_id)
    name = product.name if product else li.product_id
    variant = variants.get(li.variant_id) if li.variant_id else None
    descriptor = name
    if variant:
        descriptor += f" ({variant.name})"
    # Add-ons come from the line item's frozen snapshot — name and quantity
    # are exactly what the buyer ordered, with notes inline where present.
    addon_parts = []
    for addon in li.selected_addons or []:
        part = f"{addon['name']} x{addon['quantity']}"
        note = (addon.get("note") or "").strip()
        if note:
            part += f" (Note: {note})"
        addon_parts.append(part)
    if addon_parts:
        descriptor += f" + {', '.join(addon_parts)}"
    custom_notes = [
        f"{key}: {value}"
        for key, value in (li.customizations or {}).items()
        if str(value).strip()
    ]
    if custom_notes:
        descriptor += f" [{'; '.join(custom_notes)}]"
    return f"{descriptor} x{li.qty}"


async def _load_order_fulfilment_context(order: Order, db: AsyncSession):
    """Load line items + product/variant maps for an order, or None if no lines."""
    line_items = (
        await db.execute(
            select(OrderLineItem).where(OrderLineItem.order_id == order.id)
        )
    ).scalars().all()
    if not line_items:
        return None
    product_ids = {li.product_id for li in line_items}
    variant_ids = {li.variant_id for li in line_items if li.variant_id}
    products = {
        p.id: p for p in (
            await db.execute(select(Product).where(Product.id.in_(product_ids)))
        ).scalars().all()
    }
    variants = {
        v.id: v for v in (
            await db.execute(select(ProductVariant).where(ProductVariant.id.in_(variant_ids)))
        ).scalars().all()
    } if variant_ids else {}
    return line_items, products, variants


def _vendor_order_email_body(
    order: Order,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
    customer_phone: str,
    customer_email: str,
) -> str:
    """Full plain-text order packet for the vendor: who/where/what to ship.

    Carries everything the WhatsApp template can't: buyer contact, full address,
    per-item breakdown, gift message, the hide-prices instruction, and clickable
    upload links.
    """
    delivery = order.delivery_date.strftime("%d %b %Y") if order.delivery_date else "—"
    lines = [
        f"New Wishkr order #{order.id}",
        "",
        "── Ship to ──",
        f"Recipient: {order.shipping_name}",
        f"Phone: {order.shipping_phone}",
        f"Address: {_shipping_address_summary(order)}",
        f"Deliver on/before: {delivery}",
        "",
        "── Buyer ──",
        f"Name: {customer_name}",
        f"Phone: {customer_phone}",
        f"Email: {customer_email or '—'}",
        "",
        "── Items ──",
    ]
    upload_links: list[str] = []
    for li in line_items:
        lines.append(f"• {_render_line_descriptor(li, products, variants)}")
        for key, value in (li.customizations or {}).items():
            if _looks_like_url(str(value)):
                upload_links.append(f"{key}: {value}")

    if upload_links:
        lines += ["", "── Uploaded files ──", *upload_links]

    gift_message = (order.gift_message or "").strip()
    if gift_message:
        lines += ["", "── Gift message (include with parcel) ──", gift_message]

    if order.hide_prices:
        lines += ["", "⚠️ This is a gift — DO NOT include any price/invoice in the parcel."]

    lines += ["", "Reply to this email or message Wishkr if you have any questions."]
    return "\n".join(lines)


# ─── Branded HTML email (cream/gold/purple to match the storefront) ─────────
_EMAIL_CREAM = "#fbf8f3"
_EMAIL_CREAM2 = "#f3ece2"
_EMAIL_GOLD = "#d4a853"
_EMAIL_PURPLE = "#7b5b9a"
_EMAIL_INK = "#2a2233"
_EMAIL_MUTED = "#7a7186"
_EMAIL_LINE = "#e8e0d4"
_EMAIL_ROSE = "#e85d9c"


def _esc(value) -> str:
    return html_lib.escape(str(value))


def _product_image_url(product) -> str | None:
    """Best image for a product: explicit image_url, else first gallery image."""
    if product is None:
        return None
    if getattr(product, "image_url", None):
        return product.image_url
    images = getattr(product, "product_images", None) or []
    return images[0] if images else None


def _line_item_card_html(li, products: dict, variants: dict) -> str:
    """One product as an HTML row: thumbnail + name/variant + add-ons + customizations."""
    product = products.get(li.product_id)
    name = product.name if product else li.product_id
    variant = variants.get(li.variant_id) if li.variant_id else None
    img = _product_image_url(product)

    thumb = (
        f'<img src="{_esc(img)}" width="64" height="64" alt="" '
        f'style="width:64px;height:64px;border-radius:10px;object-fit:cover;'
        f'border:1px solid {_EMAIL_LINE};display:block;">'
        if img
        else f'<div style="width:64px;height:64px;border-radius:10px;'
        f'background:{_EMAIL_CREAM2};"></div>'
    )

    title = _esc(name)
    if variant:
        title += f' <span style="color:{_EMAIL_MUTED};font-weight:400;">({_esc(variant.name)})</span>'

    detail_bits = []
    for addon in li.selected_addons or []:
        part = f"{_esc(addon['name'])} ×{_esc(addon['quantity'])}"
        note = (addon.get("note") or "").strip()
        if note:
            part += f' <span style="color:{_EMAIL_MUTED};">(Note: {_esc(note)})</span>'
        detail_bits.append(part)
    for key, value in (li.customizations or {}).items():
        sval = str(value).strip()
        if not sval:
            continue
        if _looks_like_url(sval):
            detail_bits.append(
                f'{_esc(key)}: <a href="{_esc(sval)}" style="color:{_EMAIL_PURPLE};">view file</a>'
            )
        else:
            detail_bits.append(f"{_esc(key)}: {_esc(sval)}")

    detail_html = (
        f'<div style="margin-top:6px;font-size:13px;color:{_EMAIL_INK};line-height:1.5;">'
        + "<br>".join(detail_bits)
        + "</div>"
        if detail_bits
        else ""
    )

    return (
        f'<tr>'
        f'<td style="padding:14px 0;border-bottom:1px solid {_EMAIL_LINE};width:64px;vertical-align:top;">{thumb}</td>'
        f'<td style="padding:14px 0 14px 14px;border-bottom:1px solid {_EMAIL_LINE};vertical-align:top;">'
        f'<div style="font-size:15px;font-weight:700;color:{_EMAIL_INK};">{title}</div>'
        f'{detail_html}</td>'
        f'<td style="padding:14px 0;border-bottom:1px solid {_EMAIL_LINE};text-align:right;'
        f'font-size:14px;font-weight:700;color:{_EMAIL_INK};vertical-align:top;white-space:nowrap;">×{_esc(li.qty)}</td>'
        f'</tr>'
    )


def _email_shell(title: str, preheader: str, inner: str) -> str:
    """Wrap inner content in a centered, branded card layout."""
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{_EMAIL_CREAM2};">'
        f'<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</span>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_EMAIL_CREAM2};padding:24px 0;"><tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;background:{_EMAIL_CREAM};border-radius:16px;overflow:hidden;'
        f'border:1px solid {_EMAIL_LINE};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        f'<tr><td style="background:{_EMAIL_INK};padding:22px 28px;">'
        f'<span style="color:{_EMAIL_GOLD};font-size:22px;font-weight:800;letter-spacing:0.5px;">Wishkr</span>'
        f'<span style="color:#ffffff;font-size:14px;margin-left:10px;">{_esc(title)}</span>'
        f'</td></tr>'
        f'<tr><td style="padding:28px;">{inner}</td></tr>'
        f'<tr><td style="padding:18px 28px;background:{_EMAIL_CREAM2};color:{_EMAIL_MUTED};font-size:12px;text-align:center;">'
        f'Reply to this email or message Wishkr if you have any questions.</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def _section_heading(text: str) -> str:
    return (
        f'<div style="font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;'
        f'color:{_EMAIL_PURPLE};margin:22px 0 8px;">{_esc(text)}</div>'
    )


def _vendor_order_email_html(
    order: Order,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
    customer_phone: str,
    customer_email: str,
) -> str:
    delivery = order.delivery_date.strftime("%d %b %Y") if order.delivery_date else "—"
    rows = "".join(_line_item_card_html(li, products, variants) for li in line_items)

    def info_row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:3px 0;color:{_EMAIL_MUTED};font-size:13px;width:120px;">{_esc(label)}</td>'
            f'<td style="padding:3px 0;color:{_EMAIL_INK};font-size:13px;font-weight:600;">{_esc(value)}</td></tr>'
        )

    ship = (
        _section_heading("Ship to")
        + '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
        + info_row("Recipient", order.shipping_name or "—")
        + info_row("Phone", order.shipping_phone or "—")
        + info_row("Address", _shipping_address_summary(order))
        + info_row("Deliver by", delivery)
        + "</table>"
    )
    buyer = (
        _section_heading("Buyer")
        + '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
        + info_row("Name", customer_name)
        + info_row("Phone", customer_phone or "—")
        + info_row("Email", customer_email or "—")
        + "</table>"
    )
    items = (
        _section_heading("Items to fulfil")
        + '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">'
        + rows
        + "</table>"
    )

    gift = ""
    gift_message = (order.gift_message or "").strip()
    if gift_message:
        gift = (
            _section_heading("Gift message — include with parcel")
            + f'<div style="background:{_EMAIL_CREAM2};border-left:3px solid {_EMAIL_GOLD};'
            f'padding:12px 16px;border-radius:8px;color:{_EMAIL_INK};font-size:14px;'
            f'font-style:italic;white-space:pre-wrap;">{_esc(gift_message)}</div>'
        )

    hide = ""
    if order.hide_prices:
        hide = (
            f'<div style="margin-top:22px;background:#fff4f9;border:1px solid {_EMAIL_ROSE};'
            f'border-radius:10px;padding:14px 16px;color:{_EMAIL_ROSE};font-size:14px;font-weight:700;">'
            f'⚠️ This is a gift — do NOT include any price or invoice in the parcel.</div>'
        )

    intro = (
        f'<p style="margin:0 0 4px;font-size:16px;color:{_EMAIL_INK};">'
        f'You\'ve received a new order <strong>#{_esc(order.id)}</strong>.</p>'
    )
    return _email_shell(
        title="New order",
        preheader=f"New order #{order.id} — action needed",
        inner=intro + ship + buyer + items + gift + hide,
    )


def _buyer_confirmation_email_html(
    order: Order,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
) -> str:
    delivery = order.delivery_date.strftime("%d %b %Y") if order.delivery_date else "—"
    rows = "".join(_line_item_card_html(li, products, variants) for li in line_items)
    inner = (
        f'<p style="margin:0 0 4px;font-size:16px;color:{_EMAIL_INK};">Hi {_esc(customer_name or "there")},</p>'
        f'<p style="margin:0 0 4px;font-size:15px;color:{_EMAIL_INK};">'
        f'Thanks for your order! <strong>#{_esc(order.id)}</strong> is confirmed.</p>'
        + _section_heading("Your gift")
        + '<table role="presentation" cellpadding="0" cellspacing="0" width="100%">' + rows + "</table>"
        + f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin-top:14px;">'
        f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;width:130px;">Ship to</td>'
        f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">{_esc(_shipping_address_summary(order))}</td></tr>'
        f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;">Delivery by</td>'
        f'<td style="color:{_EMAIL_INK};font-size:13px;font-weight:600;">{_esc(delivery)}</td></tr>'
        f'<tr><td style="color:{_EMAIL_MUTED};font-size:13px;">Total</td>'
        f'<td style="color:{_EMAIL_INK};font-size:15px;font-weight:800;">₹{_esc(order.total_amount)}</td></tr>'
        f'</table>'
        f'<p style="margin:22px 0 0;font-size:14px;color:{_EMAIL_MUTED};">'
        f"We'll let you know as your gift makes its way over. — Team Wishkr</p>"
    )
    return _email_shell(
        title="Order confirmed",
        preheader=f"Your Wishkr order #{order.id} is confirmed",
        inner=inner,
    )


async def _email_vendor_order_packet(
    order: Order,
    vendor: Vendor,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
    customer_phone: str,
    customer_email: str,
) -> None:
    """Email the vendor the full order packet. Best-effort; never raises."""
    if not vendor.poc_email:
        return
    try:
        await send_email(
            to_email=vendor.poc_email,
            subject=f"New Wishkr order #{order.id} — action needed",
            text=_vendor_order_email_body(
                order, line_items, products, variants,
                customer_name, customer_phone, customer_email,
            ),
            html=_vendor_order_email_html(
                order, line_items, products, variants,
                customer_name, customer_phone, customer_email,
            ),
            reply_to=customer_email or None,
        )
    except Exception as e:
        logger.error(
            "Vendor order email failed for order=%s vendor=%s: %s",
            order.id, vendor.id, e,
        )


async def _email_buyer_confirmation(
    order: Order,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
    customer_email: str,
) -> None:
    """Send the buyer an order-confirmation email. Best-effort; never raises."""
    if not customer_email:
        return
    delivery = order.delivery_date.strftime("%d %b %Y") if order.delivery_date else "—"
    item_lines = [f"• {_render_line_descriptor(li, products, variants)}" for li in line_items]
    body = "\n".join(
        [
            f"Hi {customer_name or 'there'},",
            "",
            f"Thanks for your order with Wishkr! Order #{order.id} is confirmed.",
            "",
            "── Items ──",
            *item_lines,
            "",
            f"Ship to: {_shipping_address_summary(order)}",
            f"Expected delivery on/before: {delivery}",
            f"Total: ₹{order.total_amount}",
            "",
            "We'll let you know as your gift makes its way over.",
            "— Team Wishkr",
        ]
    )
    try:
        await send_email(
            to_email=customer_email,
            subject=f"Your Wishkr order #{order.id} is confirmed",
            text=body,
            html=_buyer_confirmation_email_html(
                order, line_items, products, variants, customer_name,
            ),
        )
    except Exception as e:
        logger.error(
            "Buyer confirmation email failed for order=%s: %s", order.id, e
        )


async def _email_team_order_notification(
    order: Order,
    vendor: Vendor | None,
    line_items,
    products: dict,
    variants: dict,
    customer_name: str,
    customer_phone: str,
    customer_email: str,
) -> None:
    """Send the Wishkr team an order notification email. Best-effort; never raises."""
    to_email = settings.order_notification_email_to
    if not to_email:
        return
    vendor_label = vendor.name if vendor else (order.vendor_id or "Unknown Vendor")
    try:
        await send_email(
            to_email=to_email,
            subject=f"[New Order] #{order.id} — ₹{order.total_amount} ({vendor_label})",
            text=_vendor_order_email_body(
                order, line_items, products, variants,
                customer_name, customer_phone, customer_email,
            ),
            html=_vendor_order_email_html(
                order, line_items, products, variants,
                customer_name, customer_phone, customer_email,
            ),
            reply_to=customer_email or None,
        )
    except Exception as e:
        logger.error(
            "Team order notification email failed for order=%s: %s", order.id, e
        )


async def _notify_vendor_of_product_order(order: Order, db: AsyncSession) -> None:
    """Send the vendor the full order detail via WhatsApp + email, email the
    buyer a confirmation, and alert the internal team (hello@wishkr.com).

    Best-effort: a notify failure must not block marking the order PAID —
    the founder alert is the fallback so the vendor still gets fulfilled
    manually if the WhatsApp send fails.

    TODO(invoice): generate a GST tax invoice PDF (invoice-number series, seller
    legal entity, HSN/GST per line) and attach it to the vendor + buyer emails.
    Deferred — needs a PDF lib and tax-compliance review.
    """
    ctx = await _load_order_fulfilment_context(order, db)
    if ctx is None:
        return
    line_items, products, variants = ctx

    user = await _get_user(db, order.user_id)
    customer_name = (user.name if user else order.shipping_name) or "Customer"
    customer_phone = (user.phone if user else order.shipping_phone) or ""
    customer_email = (user.email if user else None) or ""

    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == order.vendor_id))
    ).scalar_one_or_none()

    # Always notify the internal team so ops has a complete record of every paid order.
    await _email_team_order_notification(
        order, vendor, line_items, products, variants,
        customer_name, customer_phone, customer_email,
    )
    # Always email the buyer their confirmation.
    await _email_buyer_confirmation(
        order, line_items, products, variants, customer_name, customer_email,
    )

    if vendor is None or (not vendor.phone and not vendor.poc_email):
        logger.warning(
            "Cannot notify vendor for order=%s: vendor=%s has no phone/email on file",
            order.id, order.vendor_id,
        )
        await send_founder_alert(
            f"⚠️ Vendor notification skipped for order {order.id} "
            f"(vendor {order.vendor_id} has no phone or email on file). Notify manually."
        )
        return

    items_summary = "; ".join(
        _render_line_descriptor(li, products, variants) for li in line_items
    )
    if order.hide_prices:
        items_summary += " | ⚠️ GIFT: do not include prices in parcel"

    # Email vendor (full packet). Independent of WhatsApp so a template
    # failure still leaves a complete record in the inbox.
    await _email_vendor_order_packet(
        order, vendor, line_items, products, variants,
        customer_name, customer_phone, customer_email,
    )

    if not vendor.phone:
        # No WhatsApp number, but the email packet went out above. Record the
        # notification and stop — don't alert the founder as a failure.
        from datetime import datetime as _dt
        order.vendor_notified_at = _dt.utcnow()
        return

    try:
        await whatsapp_service.send_template(
            vendor.phone,
            template_name=TEMPLATE_VENDOR_ORDER_NOTIFICATION,
            body_params=vendor_order_notification_params(
                order_id=order.id,
                items_summary=items_summary,
                customer_name=customer_name,
                customer_phone=customer_phone,
                shipping_address=_shipping_address_summary(order),
                customer_message=order.gift_message,
                delivery_date=(
                    order.delivery_date.strftime("%d %b %Y")
                    if order.delivery_date
                    else "—"
                ),
            ),
        )
        # Record successful notification so the admin panel shows accurate status.
        from datetime import datetime as _dt
        order.vendor_notified_at = _dt.utcnow()
    except Exception as e:
        logger.error(
            "Vendor order notification failed for order=%s vendor=%s: %s",
            order.id, vendor.id, e,
        )
        await send_founder_alert(
            f"⚠️ Vendor WhatsApp notification FAILED for order {order.id} "
            f"(vendor {vendor.id}). Notify manually. Error: {str(e)[:200]}"
        )


# ─── payment.captured (product orders via Razorpay Checkout) ──────────────
async def handle_payment_captured(payload: dict, db: AsyncSession) -> dict:
    """Mark a product Order PAID after Razorpay Checkout (order_id flow).

    Voucher orders are paid via payment_link.paid instead — this handler
    only acts on order_type="product" so it can't interfere with that flow.
    """
    payment = _entity(payload, "payment")
    payment_id = payment.get("id")
    order_id = (payment.get("notes") or {}).get("order_id")

    if not order_id:
        return {"status": "no_order_id"}

    order = await _get_order(db, order_id)
    if not order:
        return {"status": "order_not_found", "order_id": order_id}

    if order.order_type != "product":
        return {"status": "ignored_not_product_order", "order_id": order.id}

    if order.razorpay_payment_id == payment_id and order.status != OrderStatus.PENDING:
        return {"status": "already_processed", "order_id": order.id}

    if order.status not in (OrderStatus.PENDING, OrderStatus.PAID):
        return {"status": "invalid_state", "order_id": order.id, "current": order.status.value}

    order.razorpay_payment_id = payment_id
    order.status = OrderStatus.PAID
    # Freeze what was actually collected at the moment of the ORIGINAL
    # payment — total_amount is live and can be mutated by later post-purchase
    # edits, so this is the only reliable anchor for computing an outstanding
    # balance after an edit raises the total.
    order.amount_collected_paise = int(round(order.total_amount * 100))
    db.add(order)
    await db.commit()

    await _notify_vendor_of_product_order(order, db)
    # Persist vendor_notified_at set during notification so the admin panel
    # reflects accurate status.
    await db.commit()

    return {"status": "ok", "order_id": order.id}


# ─── payment_link.expired ──────────────────────────────────
async def handle_payment_link_expired(payload: dict, db: AsyncSession) -> dict:
    payment_link = _entity(payload, "payment_link")
    reference_id_raw = payment_link.get("reference_id")
    reference_id = reference_id_raw if isinstance(reference_id_raw, str) else ""

    if reference_id.startswith("opl_"):
        return await handle_topup_payment_link_expired(payload, db)

    order = await _get_order(db, reference_id)
    if not order:
        return {"status": "order_not_found", "order_id": reference_id_raw}

    if order.status != OrderStatus.PENDING:
        return {"status": "ignored", "order_id": order.id, "current": order.status.value}

    order.status = OrderStatus.EXPIRED
    db.add(order)

    user = await _get_user(db, order.user_id)
    if user:
        try:
            await send_text_or_template(
                user.phone,
                msg.PAYMENT_LINK_EXPIRED.format(order_id=order.id),
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_ORDER_STATUS_UPDATE,
                body_params=[
                    _first_name(user.name),
                    order.id,
                    "your payment link expired, so the order was not charged",
                ],
            )
        except Exception as e:
            logger.error("Failed to notify user of expired link: %s", e)

    return {"status": "ok", "order_id": order.id}


# ─── payment_link.cancelled ────────────────────────────────
async def handle_payment_link_cancelled(payload: dict, db: AsyncSession) -> dict:
    payment_link = _entity(payload, "payment_link")
    reference_id_raw = payment_link.get("reference_id")
    reference_id = reference_id_raw if isinstance(reference_id_raw, str) else ""

    if reference_id.startswith("opl_"):
        return await handle_topup_payment_link_cancelled(payload, db)

    order = await _get_order(db, reference_id)
    if not order:
        return {"status": "order_not_found", "order_id": reference_id_raw}

    if order.status not in (OrderStatus.PENDING, OrderStatus.EXPIRED):
        return {"status": "ignored", "order_id": order.id, "current": order.status.value}

    order.status = OrderStatus.CANCELLED
    db.add(order)

    user = await _get_user(db, order.user_id)
    if user:
        try:
            await send_text_or_template(
                user.phone,
                msg.PAYMENT_LINK_CANCELLED.format(order_id=order.id),
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_ORDER_STATUS_UPDATE,
                body_params=[
                    _first_name(user.name),
                    order.id,
                    "the payment link was cancelled, so the order was not charged",
                ],
            )
        except Exception as e:
            logger.error("Failed to notify user of cancellation: %s", e)

    return {"status": "ok", "order_id": order.id}


# ─── payment.failed ────────────────────────────────────────
_PAYMENT_FAILURE_BURST_THRESHOLD = 3
_PAYMENT_FAILURE_BURST_WINDOW_MINUTES = 10


async def handle_payment_failed(payload: dict, db: AsyncSession) -> dict:
    payment = _entity(payload, "payment")
    payment_id = payment.get("id")
    order_id = payment.get("order_id") or payment.get("notes", {}).get("order_id")
    error_description = payment.get("error_description", "unknown")

    logger.warning(
        "Payment failed: payment_id=%s order_id=%s error=%s",
        payment_id, order_id, error_description,
    )

    # Rate-based founder alert: N failures in the last X minutes
    window_start = datetime.now(timezone.utc) - timedelta(minutes=_PAYMENT_FAILURE_BURST_WINDOW_MINUTES)
    count_result = await db.execute(
        select(func.count(WebhookEvent.id))
        .where(WebhookEvent.event_type == "payment.failed")
        .where(WebhookEvent.received_at >= window_start)
    )
    recent_failures = count_result.scalar_one()

    if recent_failures >= _PAYMENT_FAILURE_BURST_THRESHOLD:
        # Collect phones involved for the alert
        recent_result = await db.execute(
            select(WebhookEvent.payload)
            .where(WebhookEvent.event_type == "payment.failed")
            .where(WebhookEvent.received_at >= window_start)
            .limit(10)
        )
        phones: set[str] = set()
        for (p,) in recent_result.all():
            contact = (p.get("payload", {}).get("payment", {}).get("entity", {}).get("contact") or "")
            if contact:
                phones.add(contact)

        await send_founder_alert(
            msg.ALERT_PAYMENT_FAILURE_BURST.format(
                count=recent_failures + 1,
                phones=", ".join(list(phones)[:5]) or "unknown",
            )
        )

    # Notify the user that their payment failed. Template-only — by the time
    # this webhook fires, the user has likely closed WhatsApp, so the 24h CS
    # window is usually shut. The approved utility template ``payment_failed``
    # is the right vehicle. We resolve the order by our internal id stashed
    # in ``notes.order_id`` (set when we create the payment link); the
    # razorpay-internal ``payment.order_id`` is unrelated to our schema.
    order = await _get_order(db, order_id) if order_id else None
    if order:
        user = await _get_user(db, order.user_id)
        if user:
            try:
                await whatsapp_service.send_template(
                    user.phone,
                    template_name=TEMPLATE_PAYMENT_FAILED,
                    body_params=payment_failed_params(
                        sender_name=user.name,
                        order_id=order.id,
                        reason=error_description,
                    ),
                )
            except Exception as e:
                # Don't block the webhook on a notify failure; the founder
                # already got the burst alert above and can reach out manually.
                logger.error(
                    "payment_failed template send failed: order=%s phone=%s err=%s",
                    order.id, mask_phone(user.phone), e,
                )

    return {"status": "logged", "payment_id": payment_id, "error": error_description[:120]}


# ─── payment.dispute.created ───────────────────────────────
async def handle_dispute_created(payload: dict, db: AsyncSession) -> dict:
    dispute = _entity(payload, "dispute")
    payment = _entity(payload, "payment")

    dispute_id = dispute.get("id")
    payment_id = dispute.get("payment_id") or payment.get("id")
    amount_paise = dispute.get("amount", 0)
    amount_rupees = Decimal(amount_paise) / Decimal(100)
    reason_code = dispute.get("reason_code") or "unknown"
    reason_description = dispute.get("reason_description") or ""
    respond_by_ts = dispute.get("respond_by")

    # Find related order via payment_id
    order: Order | None = None
    if payment_id:
        result = await db.execute(select(Order).where(Order.razorpay_payment_id == payment_id))
        order = result.scalar_one_or_none()

    if not order:
        logger.error("Dispute created but no matching order: dispute_id=%s payment_id=%s", dispute_id, payment_id)
        # No-match disputes are MORE dangerous than matched ones — Razorpay
        # auto-loses chargebacks at 7 days if we don't respond. Alert anyway
        # so the founder can pull the dashboard manually.
        respond_by_str = (
            datetime.fromtimestamp(respond_by_ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
            if respond_by_ts else "ASAP (check Razorpay)"
        )
        try:
            await send_founder_alert(
                msg.ALERT_DISPUTE_CREATED.format(
                    order_id=f"UNMATCHED:{dispute_id}",
                    phone="unknown",
                    amount=int(amount_rupees),
                    reason=f"NO ORDER MATCH (payment_id={payment_id}): {reason_code}: {reason_description[:120]}",
                    respond_by=respond_by_str,
                )
            )
        except Exception:
            logger.exception("Failed to alert on unmatched dispute %s", dispute_id)
        return {"status": "order_not_found", "dispute_id": dispute_id}

    # Create or update Dispute record
    existing = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    d = existing.scalar_one_or_none()
    if d is None:
        d = Dispute(
            id=dispute_id,
            order_id=order.id,
            razorpay_payment_id=payment_id,
            amount=amount_rupees,
            currency=dispute.get("currency", "INR"),
            reason_code=reason_code,
            reason_description=reason_description,
            status=DisputeStatus.OPEN,
            raised_at=datetime.now(timezone.utc),
            respond_by=datetime.fromtimestamp(respond_by_ts, tz=timezone.utc) if respond_by_ts else None,
        )
        db.add(d)

    order.status = OrderStatus.DISPUTED
    db.add(order)

    user = await _get_user(db, order.user_id)

    # Notify user (calm, professional — do not imply guilt)
    if user:
        try:
            await send_text_or_template(
                user.phone,
                msg.DISPUTE_ACKNOWLEDGED.format(order_id=order.id),
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_ORDER_STATUS_UPDATE,
                body_params=[
                    _first_name(user.name),
                    order.id,
                    "we received a payment dispute and our team is reviewing it — no action needed from you right now",
                ],
            )
        except Exception as e:
            logger.error("Failed to notify user of dispute: %s", e)

    # URGENT founder alert
    respond_by_str = (
        d.respond_by.strftime("%d %b %Y %H:%M UTC") if d.respond_by else "ASAP (check Razorpay)"
    )
    await send_founder_alert(
        msg.ALERT_DISPUTE_CREATED.format(
            order_id=order.id,
            phone=user.phone if user else "unknown",
            amount=int(amount_rupees),
            reason=f"{reason_code}: {reason_description[:120]}",
            respond_by=respond_by_str,
        )
    )

    return {"status": "ok", "dispute_id": dispute_id, "order_id": order.id}


# ─── payment.dispute.won ───────────────────────────────────
async def handle_dispute_won(payload: dict, db: AsyncSession) -> dict:
    dispute = _entity(payload, "dispute")
    dispute_id = dispute.get("id")

    result = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    d = result.scalar_one_or_none()
    if not d:
        return {"status": "dispute_not_found", "dispute_id": dispute_id}

    d.status = DisputeStatus.WON
    d.resolved_at = datetime.now(timezone.utc)
    db.add(d)

    order = await _get_order(db, d.order_id)
    user = await _get_user(db, order.user_id) if order else None

    if order and order.status == OrderStatus.DISPUTED:
        # Keep it as COMPLETED since money is retained
        order.status = OrderStatus.COMPLETED
        db.add(order)

    await send_founder_alert(
        msg.ALERT_DISPUTE_WON.format(
            order_id=d.order_id,
            phone=user.phone if user else "unknown",
            amount=int(d.amount),
        )
    )

    return {"status": "ok", "dispute_id": dispute_id}


# ─── payment.dispute.lost ──────────────────────────────────
async def handle_dispute_lost(payload: dict, db: AsyncSession) -> dict:
    dispute = _entity(payload, "dispute")
    dispute_id = dispute.get("id")

    result = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    d = result.scalar_one_or_none()
    if not d:
        return {"status": "dispute_not_found", "dispute_id": dispute_id}

    d.status = DisputeStatus.LOST
    d.resolved_at = datetime.now(timezone.utc)
    db.add(d)

    order = await _get_order(db, d.order_id)
    user = await _get_user(db, order.user_id) if order else None

    if order:
        order.status = OrderStatus.REFUNDED
        db.add(order)

    await send_founder_alert(
        msg.ALERT_DISPUTE_LOST.format(
            order_id=d.order_id,
            phone=user.phone if user else "unknown",
            amount=int(d.amount),
        )
    )

    return {"status": "ok", "dispute_id": dispute_id}


# ─── payment.dispute.closed / under_review / action_required ───
async def handle_dispute_status_update(payload: dict, db: AsyncSession, new_status: DisputeStatus) -> dict:
    dispute = _entity(payload, "dispute")
    dispute_id = dispute.get("id")

    result = await db.execute(select(Dispute).where(Dispute.id == dispute_id))
    d = result.scalar_one_or_none()
    if not d:
        return {"status": "dispute_not_found", "dispute_id": dispute_id}

    d.status = new_status
    if new_status == DisputeStatus.CLOSED:
        d.resolved_at = datetime.now(timezone.utc)
    db.add(d)

    if new_status == DisputeStatus.ACTION_REQUIRED:
        order = await _get_order(db, d.order_id)
        user = await _get_user(db, order.user_id) if order else None
        await send_founder_alert(
            msg.ALERT_DISPUTE_CREATED.format(
                order_id=d.order_id,
                phone=user.phone if user else "unknown",
                amount=int(d.amount),
                reason=f"ACTION_REQUIRED: {d.reason_code}",
                respond_by=d.respond_by.strftime("%d %b %Y %H:%M UTC") if d.respond_by else "ASAP",
            )
        )

    return {"status": "ok", "dispute_id": dispute_id, "new_status": new_status.value}


# ─── refund.processed ──────────────────────────────────────
async def handle_refund_processed(payload: dict, db: AsyncSession) -> dict:
    refund = _entity(payload, "refund")
    payment_id = refund.get("payment_id")
    amount_paise = refund.get("amount", 0)
    amount_rupees = Decimal(amount_paise) / Decimal(100)

    if not payment_id:
        return {"status": "no_payment_id"}

    result = await db.execute(select(Order).where(Order.razorpay_payment_id == payment_id))
    order = result.scalar_one_or_none()
    if not order:
        logger.warning("Refund processed but no matching order: payment_id=%s", payment_id)
        return {"status": "order_not_found", "payment_id": payment_id}

    order.status = OrderStatus.REFUNDED
    db.add(order)

    # Mark purchases as refunded
    p_result = await db.execute(select(Purchase).where(Purchase.order_id == order.id))
    for p in p_result.scalars().all():
        p.status = PurchaseStatus.REFUNDED
        db.add(p)

    user = await _get_user(db, order.user_id)
    if user:
        try:
            await send_text_or_template(
                user.phone,
                msg.REFUND_PROCESSED.format(order_id=order.id, amount=int(amount_rupees)),
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_REFUND_UPDATE,
                body_params=[
                    _first_name(user.name),
                    str(int(amount_rupees)),
                    order.id,
                    "processed",
                ],
            )
        except Exception as e:
            logger.error("Failed to notify user of refund: %s", e)

    await send_founder_alert(
        msg.ALERT_REFUND_PROCESSED.format(
            order_id=order.id,
            phone=user.phone if user else "unknown",
            amount=int(amount_rupees),
        )
    )

    return {"status": "ok", "order_id": order.id, "amount": float(amount_rupees)}


# ─── refund.created ────────────────────────────────────────
async def handle_refund_created(payload: dict, db: AsyncSession) -> dict:
    refund = _entity(payload, "refund")
    payment_id = refund.get("payment_id")
    amount_paise = refund.get("amount", 0)
    amount_rupees = Decimal(amount_paise) / Decimal(100)

    if not payment_id:
        return {"status": "no_payment_id"}

    result = await db.execute(select(Order).where(Order.razorpay_payment_id == payment_id))
    order = result.scalar_one_or_none()
    if not order:
        return {"status": "order_not_found", "payment_id": payment_id}

    user = await _get_user(db, order.user_id)
    if user:
        try:
            await send_text_or_template(
                user.phone,
                msg.REFUND_INITIATED.format(order_id=order.id, amount=int(amount_rupees)),
                last_inbound_at=user.last_inbound_at,
                template_name=TEMPLATE_REFUND_UPDATE,
                body_params=[
                    _first_name(user.name),
                    str(int(amount_rupees)),
                    order.id,
                    "initiated",
                ],
            )
        except Exception as e:
            logger.error("Failed to notify user of refund initiation: %s", e)

    return {"status": "ok", "order_id": order.id}


# ─── refund.failed ─────────────────────────────────────────
# Customer was promised a refund (we sent REFUND_INITIATED). Bank/UPI rejected it.
# If we stay silent, customer files a chargeback — auto-loss in 7 days.
async def handle_refund_failed(payload: dict, db: AsyncSession) -> dict:
    refund = _entity(payload, "refund")
    payment_id = refund.get("payment_id")
    amount_paise = refund.get("amount", 0)
    amount_rupees = Decimal(amount_paise) / Decimal(100)
    reason = refund.get("error_description") or refund.get("status_details", {}).get("description") or "unknown"

    if not payment_id:
        return {"status": "no_payment_id"}

    result = await db.execute(select(Order).where(Order.razorpay_payment_id == payment_id))
    order = result.scalar_one_or_none()
    if not order:
        logger.error("Refund failed but no matching order: payment_id=%s", payment_id)
        return {"status": "order_not_found", "payment_id": payment_id}

    order.status = OrderStatus.REFUND_FAILED
    db.add(order)

    user = await _get_user(db, order.user_id)
    # Deliberately do NOT auto-message the user — founder handles outreach manually
    # to avoid a bad template worsening the situation.
    await send_founder_alert(
        msg.ALERT_REFUND_FAILED.format(
            order_id=order.id,
            phone=user.phone if user else "unknown",
            amount=int(amount_rupees),
            reason=str(reason)[:200],
        )
    )

    return {"status": "ok", "order_id": order.id, "reason": str(reason)[:120]}


# ─── payment_link.partially_paid ───────────────────────────
# Customer paid less than the link amount — voucher cannot be issued.
# Money is sitting with us; silence = legal exposure.
async def handle_payment_link_partially_paid(payload: dict, db: AsyncSession) -> dict:
    payment_link = _entity(payload, "payment_link")
    reference_id_raw = payment_link.get("reference_id")
    reference_id = reference_id_raw if isinstance(reference_id_raw, str) else ""
    expected_paise = payment_link.get("amount", 0)
    received_paise = payment_link.get("amount_paid", 0)

    order = await _get_order(db, reference_id)
    if not order:
        return {"status": "order_not_found", "order_id": reference_id_raw}

    order.status = OrderStatus.PARTIALLY_PAID
    db.add(order)

    user = await _get_user(db, order.user_id)
    await send_founder_alert(
        msg.ALERT_PARTIAL_PAYMENT.format(
            order_id=order.id,
            phone=user.phone if user else "unknown",
            expected=int(Decimal(expected_paise) / Decimal(100)),
            received=int(Decimal(received_paise) / Decimal(100)),
        )
    )

    return {"status": "ok", "order_id": order.id}


# ─── payment.downtime.started ──────────────────────────────
async def handle_payment_downtime_started(payload: dict, db: AsyncSession) -> dict:
    downtime = _entity(payload, "payment.downtime")
    method = downtime.get("method", "unknown")
    severity = downtime.get("severity", "unknown")
    began_at_ts = downtime.get("begin")
    instrument = downtime.get("instrument", {}) or {}

    began_at_str = (
        datetime.fromtimestamp(began_at_ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
        if began_at_ts else "unknown"
    )
    instrument_line = ""
    if instrument:
        bank = instrument.get("bank") or instrument.get("issuer") or instrument.get("psp")
        if bank:
            instrument_line = f"Affected: {bank}\n"

    await send_founder_alert(
        msg.ALERT_PAYMENT_DOWNTIME_STARTED.format(
            method=method,
            severity=severity,
            began_at=began_at_str,
            instrument_line=instrument_line,
        )
    )

    return {"status": "logged", "method": method, "severity": severity}


# ─── payment.downtime.updated ──────────────────────────────
# Quiet log — state transitions during an ongoing downtime.
# Audit row captures everything; no alert noise.
async def handle_payment_downtime_updated(payload: dict, db: AsyncSession) -> dict:
    downtime = _entity(payload, "payment.downtime")
    logger.info(
        "Payment downtime updated: method=%s severity=%s",
        downtime.get("method"), downtime.get("severity"),
    )
    return {"status": "logged"}


# ─── payment.downtime.resolved ─────────────────────────────
async def handle_payment_downtime_resolved(payload: dict, db: AsyncSession) -> dict:
    downtime = _entity(payload, "payment.downtime")
    method = downtime.get("method", "unknown")
    began_at_ts = downtime.get("begin")
    resolved_at_ts = downtime.get("end")

    began_at_str = (
        datetime.fromtimestamp(began_at_ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
        if began_at_ts else "unknown"
    )
    resolved_at_str = (
        datetime.fromtimestamp(resolved_at_ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")
        if resolved_at_ts else "now"
    )

    await send_founder_alert(
        msg.ALERT_PAYMENT_DOWNTIME_RESOLVED.format(
            method=method,
            began_at=began_at_str,
            resolved_at=resolved_at_str,
        )
    )

    return {"status": "logged", "method": method}


# ─── settlement.processed ──────────────────────────────────
# Quiet audit-only handler. Money from Razorpay actually hit our bank account.
# Essential for GST reconciliation + accounting paper trail. No user/founder message.
async def handle_settlement_processed(payload: dict, db: AsyncSession) -> dict:
    settlement = _entity(payload, "settlement")
    amount_paise = settlement.get("amount", 0)
    amount_rupees = Decimal(amount_paise) / Decimal(100)
    logger.info(
        "Settlement processed: id=%s amount=%s utr=%s",
        settlement.get("id"), amount_rupees, settlement.get("utr"),
    )
    return {"status": "logged", "amount": float(amount_rupees)}
