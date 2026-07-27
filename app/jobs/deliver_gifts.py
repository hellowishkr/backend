"""Cron job: deliver held vouchers to celebrants on their occasion date."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import async_session
from app.models import (
    Purchase, PurchaseStatus, DeliveryStatus, GiftType,
    Order, Occasion, User,
)
from app.bot import messages as msg
from app.bot.whatsapp_templates import (
    TEMPLATE_ACCOUNT_NOTIFICATION,
    TEMPLATE_GIFT_DELIVERED_UPDATE,
    TEMPLATE_VOUCHER_DELIVERED_FRIEND,
    TEMPLATE_WISHKR_INVITE,
    _first_name,
)
from app.models.occasion import occasion_phrase
from app.services.hubble import HubbleOrderError
from app.services.hubble_fulfillment import issue_hubble_voucher
from app.services.fulfillment_policy import FulfillmentPolicy
from app.services.whatsapp import whatsapp_service, is_in_cs_window, send_text_or_template
from app.core.config import settings
from app.models.order import OrderStatus
from app.utils.alerts import send_founder_alert
from app.utils.observability import mask_phone
from app.webhooks.whatsapp import record_failed_outbound_message

# Shown in templated send when the buyer didn't add a personal note. The
# template body has a quoted "{{4}}" placeholder so we must put *something*
# there — Meta rejects empty params.
_DEFAULT_PERSONAL_MESSAGE = "Wishing you the best 🎁"

logger = logging.getLogger("wishkr.jobs.deliver_gifts")


async def deliver_scheduled_gifts():
    """Find due fulfillment rows and deliver gifts with isolated transactions."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)

        # Query by the provider-neutral fulfillment contract. Today only Hubble
        # gift cards have an automatic provider handler; other due providers
        # are moved to manual review inside `_process_due_purchase`.
        result = await db.execute(
            select(Purchase.id).where(
                Purchase.delivery_status == DeliveryStatus.PENDING_DELIVERY,
                Purchase.fulfillment_due_at <= now,
                Purchase.fulfillment_policy != FulfillmentPolicy.MANUAL_REVIEW.value,
                Purchase.status.in_([PurchaseStatus.PENDING, PurchaseStatus.VOUCHER_ISSUED]),
            )
            .order_by(Purchase.fulfillment_due_at.asc(), Purchase.created_at.asc())
            .limit(50)
        )
        purchase_ids = result.scalars().all()

        if not purchase_ids:
            return

        logger.info("Delivering %d scheduled gifts", len(purchase_ids))

    await asyncio.gather(*[_process_due_purchase(pid) for pid in purchase_ids])


def _can_auto_fulfill_with_hubble(purchase: Purchase) -> bool:
    provider = purchase.provider or "hubble"
    return provider == "hubble" and purchase.gift_type == GiftType.GIFT_CARD


async def _mark_manual_review(db, purchase: Purchase, order: Order | None, reason: str) -> None:
    logger.error("Purchase %s needs manual review: %s", purchase.id, reason)
    purchase.delivery_status = DeliveryStatus.FAILED_DELIVERY
    db.add(purchase)
    if order:
        order.status = OrderStatus.NEEDS_MANUAL_REVIEW
        db.add(order)
    await db.commit()
    try:
        await send_founder_alert(
            msg.ALERT_FAILED_ORDER.format(
                order_id=order.id if order else purchase.order_id,
                phone=purchase.buyer_phone or "unknown",
                amount=int(purchase.amount),
                brand=purchase.brand_name or "Unknown",
                error=reason[:200],
            )
        )
    except Exception:
        logger.exception("Founder alert failed for purchase=%s", purchase.id)


async def _process_due_purchase(purchase_id: str) -> None:
    async with async_session() as db:
        try:
            result = await db.execute(
                select(Purchase)
                .where(Purchase.id == purchase_id)
                .with_for_update(skip_locked=True)
            )
            purchase = result.scalar_one_or_none()
            if not purchase:
                # Either disappeared or claimed by another worker — skip silently.
                return
            if purchase.delivery_status != DeliveryStatus.PENDING_DELIVERY:
                return

            order = await db.get(Order, purchase.order_id)
            if not order:
                await _mark_manual_review(db, purchase, None, "Order not found")
                return

            if not _can_auto_fulfill_with_hubble(purchase):
                await _mark_manual_review(
                    db,
                    purchase,
                    order,
                    f"No automatic fulfillment adapter for provider={purchase.provider!r}",
                )
                return

            if purchase.status == PurchaseStatus.PENDING:
                try:
                    await issue_hubble_voucher(purchase)
                    db.add(purchase)
                    await db.commit()
                except HubbleOrderError as e:
                    logger.error("Scheduled Hubble order failed for purchase %s: %s", purchase.id, e)
                    purchase.status = PurchaseStatus.FAILED
                    purchase.delivery_status = DeliveryStatus.FAILED_DELIVERY
                    order.status = OrderStatus.NEEDS_MANUAL_REVIEW
                    db.add(purchase)
                    db.add(order)
                    await db.commit()
                    if purchase.buyer_phone:
                        try:
                            # Cron runs on the occasion date — usually days
                            # after the buyer last messaged us, so the CS
                            # window is typically closed.
                            buyer_row = (await db.execute(
                                select(User.name, User.last_inbound_at)
                                .where(User.phone == purchase.buyer_phone)
                            )).first()
                            await send_text_or_template(
                                purchase.buyer_phone,
                                msg.VOUCHER_FAILED,
                                last_inbound_at=buyer_row.last_inbound_at if buyer_row else None,
                                template_name=TEMPLATE_ACCOUNT_NOTIFICATION,
                                body_params=[
                                    _first_name(buyer_row.name if buyer_row else None),
                                    msg.VOUCHER_FAILED,
                                ],
                            )
                        except Exception:
                            logger.exception(
                                "Buyer failure notification failed phone=%s",
                                mask_phone(purchase.buyer_phone),
                            )
                    try:
                        await send_founder_alert(
                            msg.ALERT_FAILED_ORDER.format(
                                order_id=order.id,
                                phone=purchase.buyer_phone or "unknown",
                                amount=int(purchase.amount),
                                brand=purchase.brand_name or "Unknown",
                                error=str(e)[:200],
                            )
                        )
                    except Exception:
                        logger.exception("Founder alert failed for purchase=%s", purchase.id)
                    return

            occ_result = await db.execute(select(Occasion).where(Occasion.id == purchase.occasion_id))
            occasion = occ_result.scalar_one_or_none()
            if not occasion:
                await _mark_manual_review(db, purchase, order, "Occasion not found")
                return

            celebrant_result = await db.execute(select(User).where(User.id == occasion.user_id))
            celebrant = celebrant_result.scalar_one_or_none()
            if not celebrant:
                await _mark_manual_review(db, purchase, order, "Celebrant not found")
                return

            sender_name = "A friend"
            if purchase.buyer_phone:
                buyer_result = await db.execute(select(User).where(User.phone == purchase.buyer_phone))
                buyer = buyer_result.scalar_one_or_none()
                if buyer and buyer.name:
                    sender_name = buyer.name

            brand = purchase.brand_name or "Gift Card"
            amount_int = int(purchase.amount)
            code = purchase.voucher_code or "N/A"
            pin = purchase.voucher_pin or "N/A"
            expiry_str = (
                purchase.voucher_expiry.strftime("%d %b %Y")
                if purchase.voucher_expiry else "Check brand T&C"
            )
            phrase = occasion_phrase(occasion.type, occasion.custom_label)
            personal_message = (purchase.personal_message or "").strip() or _DEFAULT_PERSONAL_MESSAGE

            if purchase.personal_message:
                gift_msg = msg.GIFT_RECEIVED.format(
                    sender_name=sender_name,
                    brand=brand,
                    amount=amount_int,
                    message=purchase.personal_message,
                    code=code,
                    pin=pin,
                    expiry=expiry_str,
                )
            else:
                gift_msg = msg.GIFT_RECEIVED_NO_MESSAGE.format(
                    sender_name=sender_name,
                    brand=brand,
                    amount=amount_int,
                    code=code,
                    pin=pin,
                    expiry=expiry_str,
                )

            # Commit the durable delivered state before sending. That prevents
            # a worker crash or Meta retry from causing a voucher double-send.
            purchase.delivery_status = DeliveryStatus.DELIVERED
            purchase.status = PurchaseStatus.DELIVERED
            order.status = OrderStatus.COMPLETED
            db.add(purchase)
            db.add(order)
            await db.commit()

            use_template = (
                settings.whatsapp_use_voucher_template
                and not is_in_cs_window(celebrant.last_inbound_at)
            )
            template_header = [sender_name]
            template_body = [
                sender_name,
                str(amount_int),
                phrase,
                personal_message,
                code,
                pin,
                expiry_str,
            ]
            try:
                if use_template:
                    await whatsapp_service.send_template(
                        celebrant.phone,
                        TEMPLATE_VOUCHER_DELIVERED_FRIEND,
                        header_params=template_header,
                        body_params=template_body,
                    )
                else:
                    await whatsapp_service.send_text(celebrant.phone, gift_msg)
            except Exception as e:
                logger.error(
                    "deliver_gifts: send failed after status commit purchase=%s err=%s",
                    purchase.id, e,
                )
                await record_failed_outbound_message(
                    celebrant.phone, gift_msg, "deliver_gift", str(e),
                    template_name=TEMPLATE_VOUCHER_DELIVERED_FRIEND if use_template else None,
                    template_params=(
                        {"header_params": template_header, "body_params": template_body}
                        if use_template else None
                    ),
                )

            logger.info("Gift delivered: purchase=%s, celebrant=%s, sender=%s",
                        purchase.id, mask_phone(celebrant.phone), sender_name)

            try:
                # Out-of-window celebrants (often cold contacts in the friend
                # gifting flow) get the wishkr_invite Marketing template
                # instead of a silently-dropped free text.
                await send_text_or_template(
                    celebrant.phone,
                    msg.VIRAL_AFTER_GIFT_RECEIVED,
                    last_inbound_at=celebrant.last_inbound_at,
                    template_name=TEMPLATE_WISHKR_INVITE,
                    body_params=[_first_name(celebrant.name)],
                )
            except Exception as e:
                logger.warning(
                    "viral_after_received send failed phone=%s err=%s",
                    mask_phone(celebrant.phone), e,
                )
                await record_failed_outbound_message(
                    celebrant.phone, msg.VIRAL_AFTER_GIFT_RECEIVED,
                    "viral_after_received", str(e),
                )

            if purchase.buyer_phone:
                try:
                    # In-window the free text doubles as delivery confirmation
                    # + referral ask. Out-of-window only the delivery
                    # confirmation survives, as a Utility template — the buyer
                    # paid and must learn the gift card arrived.
                    await send_text_or_template(
                        purchase.buyer_phone,
                        msg.VIRAL_AFTER_GIFT_SENT,
                        last_inbound_at=buyer.last_inbound_at if buyer else None,
                        template_name=TEMPLATE_GIFT_DELIVERED_UPDATE,
                        body_params=[
                            _first_name(buyer.name if buyer else None),
                            celebrant.name or "your friend",
                        ],
                    )
                except Exception as e:
                    logger.warning(
                        "viral_after_sent send failed phone=%s err=%s",
                        mask_phone(purchase.buyer_phone), e,
                    )
                    await record_failed_outbound_message(
                        purchase.buyer_phone, msg.VIRAL_AFTER_GIFT_SENT,
                        "viral_after_sent", str(e),
                    )

        except Exception as e:
            await db.rollback()
            logger.error("Failed to deliver gift for purchase %s: %s", purchase_id, e)
