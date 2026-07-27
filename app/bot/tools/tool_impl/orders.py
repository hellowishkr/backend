import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    User, Occasion, Order, OrderStatus,
    Purchase, GiftType, PurchaseStatus, DeliveryStatus,
    CelebrantOccasion, CelebrantFriend,
)
from app.services.razorpay_service import razorpay_service
from app.services.fulfillment_policy import apply_fulfillment_defaults
from app.services.checkout_quotes import CheckoutQuote
from app.utils.id_gen import generate_id
from app.utils.platform_fee import calculate_platform_fee
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_REGISTER, _HINT_OCCASION_UNKNOWN, _HINT_BRAND_UNKNOWN, _HINT_CELEBRANT_UNKNOWN,
    _get_user, _find_brand_id, _check_brand_amount, _user_owns_occasion, _duplicate_brand_warning,
)
from app.bot.state_utils import get_state, has_active_gift_context, update_active_gift_context

logger = logging.getLogger("wishkr.tools.functions")

# Razorpay link expiry mirrors what we send to Razorpay (now+30min). Use a
# slightly tighter window for reuse so we never hand the user a link that's
# about to die in their hands.
_LINK_REUSE_WINDOW_SECONDS = 25 * 60


async def _quote_hubble_gift_card(
    db: AsyncSession,
    brand_name: str,
    amount: int,
) -> CheckoutQuote | dict:
    """Resolve and validate the current Hubble gift-card checkout quote.

    This is the compatibility bridge between today's brand/amount tools and
    the provider-neutral checkout path. Future selected-candidate checkout can
    return the same ``CheckoutQuote`` shape without threading Hubble-specific
    IDs through order creation.
    """
    brand_id = await _find_brand_id(brand_name, db)
    if not brand_id:
        return _err("brand_not_found", _HINT_BRAND_UNKNOWN, brand_name=brand_name)

    denom_error = await _check_brand_amount(brand_id, brand_name, amount)
    if denom_error:
        return denom_error

    return CheckoutQuote(
        provider="hubble",
        provider_product_id=brand_id,
        display_name=brand_name,
        amount=int(amount),
        fulfillment_mode="api",
    )


async def _cancel_stale_pending_orders(
    db: AsyncSession,
    user_id: str,
    brand_name: str,
    amount,
    *,
    occasion_id: str | None = None,
) -> dict | None:
    """Cancel older PENDING orders for the same intent before minting a new
    link. Without this, an aged-out link still live on Razorpay's side
    (their 30-min window) can be paid in parallel with the freshly issued
    link — double-charging the user and triggering two Hubble fulfillments.

    We cancel on Razorpay first (so the link can't be paid) and then mark
    the local Order as CANCELLED.

    Returns a structured error dict if any candidate is in a dangerous state
    (link already paid, or Razorpay state unconfirmable). Callers must not
    mint a new payment link when a non-None value is returned.
    """
    result = await db.execute(
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.status == OrderStatus.PENDING,
        )
    )
    candidates = list(result.scalars().all())
    for order in candidates:
        p_result = await db.execute(
            select(Purchase).where(Purchase.order_id == order.id)
        )
        purchases = list(p_result.scalars().all())
        if len(purchases) != 1:
            continue
        p = purchases[0]
        if p.brand_name != brand_name:
            continue
        if float(p.amount) != float(amount):
            continue
        if (p.occasion_id or None) != (occasion_id or None):
            continue

        if order.razorpay_link_id:
            try:
                await razorpay_service.cancel_payment_link(order.razorpay_link_id)
            except Exception as e:
                # Cancel can fail because (a) Razorpay refused (already
                # cancelled / expired — harmless) or (b) the link was already
                # PAID. Case (b) is dangerous: marking the local order
                # CANCELLED then minting a new link gets the user charged
                # twice. Re-query state and return an error to block minting.
                logger.warning(
                    "Razorpay cancel failed for order=%s link=%s: %s",
                    order.id, order.razorpay_link_id, e,
                )
                try:
                    fresh = await razorpay_service.fetch_payment_link(order.razorpay_link_id)
                    fresh_status = (fresh or {}).get("status")
                    if fresh_status == "paid":
                        # The payment_link.paid webhook is the source of truth.
                        # Surface a user-visible error so they know payment was
                        # received and fulfillment is in progress.
                        logger.error(
                            "Refusing to cancel order=%s — Razorpay link is already PAID; "
                            "letting webhook fulfill",
                            order.id,
                        )
                        try:
                            from app.utils.alerts import send_founder_alert
                            await send_founder_alert(
                                f"⚠️ Stale-cancel race on order {order.id}: link is PAID. "
                                f"Webhook will fulfill; user warned to wait."
                            )
                        except Exception:
                            pass
                        return {
                            "error": "payment_already_received",
                            "order_id": order.id,
                            "reason": (
                                "Your payment for this order was already received. "
                                "Fulfillment is in progress — you will receive the gift card shortly."
                            ),
                            "required_action": (
                                "Tell the user their payment was already received for this order "
                                "and fulfillment is underway. Do not create a new order."
                            ),
                            "retryable": False,
                        }
                except Exception as fetch_err:
                    # Could not confirm state — refuse to mint a new link.
                    # Better to surface a transient error than risk a double-charge.
                    logger.error(
                        "Could not confirm Razorpay link state for order=%s: %s — blocking new link",
                        order.id, fetch_err,
                    )
                    return {
                        "error": "payment_state_unknown",
                        "order_id": order.id,
                        "reason": (
                            "Could not confirm the payment status of your previous order. "
                            "Please wait a moment and try again."
                        ),
                        "required_action": (
                            "Tell the user we could not confirm their previous payment status "
                            "and ask them to try again in a minute."
                        ),
                        "retryable": True,
                    }
        order.status = OrderStatus.CANCELLED
        db.add(order)
    await db.flush()
    return None


async def _find_reusable_pending_order(
    db: AsyncSession,
    user_id: str,
    brand_name: str,
    amount,
    *,
    occasion_id: str | None = None,
) -> tuple[Order, Purchase] | None:
    """Find a still-live PENDING order for the same intent (user + brand +
    amount + occasion). Returns the Order/Purchase pair or None.

    "Same intent" means: same user, the order's single Purchase row matches
    on brand_name + amount + occasion_id, and the order was created recently
    enough that the Razorpay link hasn't expired. We deliberately scope to
    one Purchase per Order — the only callers (create_gift_order /
    create_friend_gift_order) only ever attach one Purchase, so a multi-line
    order is by definition a different intent.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_LINK_REUSE_WINDOW_SECONDS)
    result = await db.execute(
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.status == OrderStatus.PENDING,
            Order.created_at >= cutoff,
            Order.razorpay_short_url.is_not(None),
        )
        .order_by(Order.created_at.desc())
        .limit(5)
    )
    candidates = list(result.scalars().all())
    for order in candidates:
        p_result = await db.execute(
            select(Purchase).where(Purchase.order_id == order.id)
        )
        purchases = list(p_result.scalars().all())
        if len(purchases) != 1:
            continue
        p = purchases[0]
        if p.brand_name != brand_name:
            continue
        # Numeric(10,2) → Decimal; coerce both sides for safe equality.
        if float(p.amount) != float(amount):
            continue
        if (p.occasion_id or None) != (occasion_id or None):
            continue
        return order, p
    return None


async def create_gift_order(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create a gift card order and generate a Razorpay payment link."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    brand_name = args.get("brand_name", "")
    amount = args.get("amount", 0)
    occasion_id = args.get("occasion_id")
    wishlist_item_id = (args.get("wishlist_item_id") or "").strip() or None
    duplicate_acknowledged = bool(args.get("duplicate_acknowledged"))

    if not brand_name or amount <= 0:
        return {"error": "Brand name and valid amount are required"}

    # Ownership: when the LLM tags this order to an occasion, the caller must
    # own it. Without this, a buyer can pollute another user's
    # check_occasion_gifts checklist (the dedup oracle) and probe occasion
    # existence via the response shape. Same pattern as add_to_cart's guard.
    if occasion_id and not await _user_owns_occasion(db, occasion_id, user.id):
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    if occasion_id and not duplicate_acknowledged:
        warning = await _duplicate_brand_warning(db, occasion_id, brand_name)
        if warning:
            return warning

    quote = await _quote_hubble_gift_card(db, brand_name, amount)
    if isinstance(quote, dict):
        return quote
    quote_error = quote.error
    if quote_error:
        return quote_error

    active_state = await get_state(db, phone)
    if has_active_gift_context(active_state):
        await update_active_gift_context(
            db,
            phone,
            {
                "selected_brand": brand_name,
                "budget_max": amount,
                "current_step": "brand_selected",
            },
        )

    # Idempotent reuse: if the user already has a live PENDING order for
    # the same brand + amount + occasion, return the existing link instead
    # of minting a new one. Without this, a "give me the link again" reply
    # creates a duplicate Order — and if both links get paid, the user is
    # double-charged AND we double-fulfill the gift on Hubble.
    existing = await _find_reusable_pending_order(
        db, user.id, brand_name, amount, occasion_id=occasion_id,
    )
    if existing is not None:
        order, _purchase = existing
        logger.info(
            "create_gift_order idempotent reuse: order=%s user=%s brand=%s amount=%s",
            order.id, user.id, brand_name, amount,
        )
        return {
            "success": True,
            "order_id": order.id,
            "brand": brand_name,
            "gift_amount": float(_purchase.amount),
            "total_amount": float(order.total_amount),
            "payment_link": order.razorpay_short_url,
            "expires_in": "30 minutes",
            "reused": True,
        }

    # No live link to reuse but there may be stale PENDING orders for this
    # exact intent (older than the reuse window but still inside Razorpay's
    # 30-min link expiry). Cancel them so they can't be paid in parallel
    # with the new link.
    cancel_error = await _cancel_stale_pending_orders(
        db, user.id, brand_name, amount, occasion_id=occasion_id,
    )
    if cancel_error:
        return cancel_error

    fee_breakdown = calculate_platform_fee(amount)

    order_id = generate_id("ord")
    order = Order(
        id=order_id,
        user_id=user.id,
        gift_amount=fee_breakdown["gift_amount"],
        platform_fee=fee_breakdown["platform_fee"],
        platform_fee_gst=fee_breakdown["platform_fee_gst"],
        total_amount=fee_breakdown["total_amount"],
        status=OrderStatus.PENDING,
    )
    db.add(order)

    purchase = Purchase(
        id=generate_id("pur"), order_id=order_id, occasion_id=occasion_id,
        gift_type=GiftType.GIFT_CARD, brand_name=brand_name,
        hubble_product_id=quote.provider_product_id, amount=quote.amount, status=PurchaseStatus.PENDING,
        wishlist_item_id=wishlist_item_id,
    )
    apply_fulfillment_defaults(
        purchase,
        provider_product_id=quote.provider_product_id,
        recipient_user_id=user.id if not occasion_id else None,
        recipient_phone=user.phone if not occasion_id else None,
    )
    db.add(purchase)
    await db.flush()

    # Create Razorpay payment link
    try:
        # Razorpay rejects expire_by < now+15min on their side. Computing
        # exactly now+15min races: by the time the request lands, network
        # latency + clock skew tips it under the floor and we get
        # ``BadRequestError: expire_by: timestamp must be atleast 15
        # minutes in future``. 30 minutes gives Razorpay a safe buffer
        # while still being a sensible payment-link expiry.
        expire_by = int(time.time()) + (30 * 60)
        total = fee_breakdown["total_amount"]
        platform_fee = fee_breakdown["platform_fee"]
        platform_fee_gst = fee_breakdown["platform_fee_gst"]
        fee_total = platform_fee + platform_fee_gst
        result = await razorpay_service.create_payment_link(
            amount=int(total * 100),
            description=f"{brand_name} Gift Card ₹{amount} + ₹{fee_total:.2f} gateway fee incl. GST",
            reference_id=order_id,
            customer_name=user.name or "Wishkr User",
            customer_phone=phone,
            expire_by=expire_by,
        )
        link_url = result.get("short_url", "")
        order.razorpay_link_id = result.get("id", "")
        order.razorpay_short_url = link_url
        db.add(order)
        await db.flush()

        return {
            "success": True,
            "order_id": order_id,
            "brand": brand_name,
            "gift_amount": quote.amount,
            "provider": quote.provider,
            "provider_product_id": quote.provider_product_id,
            "platform_fee": float(fee_breakdown["platform_fee"]),
            "platform_fee_gst": float(fee_breakdown["platform_fee_gst"]),
            "total_amount": float(total),
            "fee_label": fee_breakdown["fee_label"],
            "payment_slip": fee_breakdown["payment_slip"],
            "payment_link": link_url,
            "expires_in": "30 minutes",
        }
    except Exception as e:
        logger.error("Razorpay link creation failed: %s", e)
        order.status = OrderStatus.FAILED
        purchase.status = PurchaseStatus.FAILED
        db.add(order)
        db.add(purchase)
        await db.flush()
        return {"error": "Could not create payment link. Please try again."}


async def list_orders(db: AsyncSession, phone: str, args: dict) -> dict:
    """Get the user's recent orders with status and details."""
    user = await _get_user(db, phone)
    if not user:
        return _err("user_not_registered", _HINT_REGISTER)

    result = await db.execute(
        select(Order)
        .where(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .limit(10)
    )
    orders = result.scalars().all()

    if not orders:
        return {"orders": [], "message": "No orders yet"}

    items = []
    for order in orders:
        p_result = await db.execute(select(Purchase).where(Purchase.order_id == order.id))
        purchases = p_result.scalars().all()

        for p in purchases:
            items.append({
                "order_id": order.id,
                "brand": p.brand_name,
                "amount": float(p.amount),
                "status": order.status.value,
                "date": order.created_at.strftime("%d %B %Y") if order.created_at else "",
            })

    return {"orders": items}


async def create_friend_gift_order(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create a gift order from a friend to a celebrant."""
    cel_occ_id = args.get("celebrant_occasion_id")
    brand_name = args.get("brand_name", "")
    amount = args.get("amount", 0)
    buyer_name = args.get("buyer_name", "")
    personal_message = args.get("personal_message")
    wishlist_item_id = (args.get("wishlist_item_id") or "").strip() or None
    duplicate_acknowledged = bool(args.get("duplicate_acknowledged"))

    if not cel_occ_id or not brand_name or amount <= 0 or not buyer_name:
        return {"error": "celebrant_occasion_id, brand_name, amount, and buyer_name are required"}

    # Get occasion context
    result = await db.execute(select(CelebrantOccasion).where(CelebrantOccasion.id == cel_occ_id))
    cel_occ = result.scalar_one_or_none()
    if not cel_occ:
        return _err("celebrant_occasion_not_found", _HINT_CELEBRANT_UNKNOWN)

    # Authorization: only an invited friend may buy on this celebrant occasion.
    # The buyer's phone is trusted (webhook), but cel_occ_id is LLM-supplied —
    # so without this check the LLM could be coaxed into creating an order on
    # an unrelated occasion (and reveal "occasion exists" via response shape).
    friend_row = await db.execute(
        select(CelebrantFriend).where(
            CelebrantFriend.celebrant_occasion_id == cel_occ.id,
            CelebrantFriend.phone == phone,
        )
    )
    if friend_row.scalar_one_or_none() is None:
        return _err("not_invited", _HINT_CELEBRANT_UNKNOWN)

    occ_result = await db.execute(select(Occasion).where(Occasion.id == cel_occ.occasion_id))
    occasion = occ_result.scalar_one_or_none()
    if not occasion:
        return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN)

    celebrant_result = await db.execute(select(User).where(User.id == occasion.user_id))
    celebrant = celebrant_result.scalar_one_or_none()
    celebrant_name = celebrant.name if celebrant else "Your friend"

    if not duplicate_acknowledged:
        warning = await _duplicate_brand_warning(db, occasion.id, brand_name)
        if warning:
            warning["for"] = celebrant_name
            return warning

    # Find or create guest user for the buyer
    buyer_result = await db.execute(select(User).where(User.phone == phone))
    buyer = buyer_result.scalar_one_or_none()
    if not buyer:
        buyer = User(id=generate_id("usr"), phone=phone, name=buyer_name, is_guest=True)
        db.add(buyer)
        await db.flush()
    elif not buyer.name:
        buyer.name = buyer_name
        db.add(buyer)
        await db.flush()

    quote = await _quote_hubble_gift_card(db, brand_name, amount)
    if isinstance(quote, dict):
        return quote
    quote_error = quote.error
    if quote_error:
        return quote_error

    active_state = await get_state(db, phone)
    if has_active_gift_context(active_state):
        await update_active_gift_context(
            db,
            phone,
            {
                "selected_brand": brand_name,
                "budget_max": amount,
                "current_step": "brand_selected",
            },
        )

    # Idempotent reuse — see note in create_gift_order. Friend orders carry
    # an occasion_id so the lookup scopes to the same celebrant occasion.
    existing = await _find_reusable_pending_order(
        db, buyer.id, brand_name, amount, occasion_id=occasion.id,
    )
    if existing is not None:
        order, _purchase = existing
        logger.info(
            "create_friend_gift_order idempotent reuse: order=%s buyer=%s brand=%s amount=%s",
            order.id, buyer.id, brand_name, amount,
        )
        label = occasion.custom_label or occasion.type.value.replace("_", " ").title()
        return {
            "success": True,
            "order_id": order.id,
            "brand": brand_name,
            "gift_amount": float(_purchase.amount),
            "total_amount": float(order.total_amount),
            "for": celebrant_name,
            "occasion": label,
            "delivery_date": occasion.date.strftime("%d %B"),
            "payment_link": order.razorpay_short_url,
            "expires_in": "30 minutes",
            "personal_message": personal_message or "(none)",
            "reused": True,
        }

    # Cancel any stale same-intent PENDING orders before minting a new
    # link — see note in create_gift_order.
    cancel_error = await _cancel_stale_pending_orders(
        db, buyer.id, brand_name, amount, occasion_id=occasion.id,
    )
    if cancel_error:
        return cancel_error

    # Create order + purchase with scheduled delivery
    fee_breakdown = calculate_platform_fee(amount)

    order_id = generate_id("ord")
    order = Order(
        id=order_id,
        user_id=buyer.id,
        gift_amount=fee_breakdown["gift_amount"],
        platform_fee=fee_breakdown["platform_fee"],
        platform_fee_gst=fee_breakdown["platform_fee_gst"],
        total_amount=fee_breakdown["total_amount"],
        status=OrderStatus.PENDING,
    )
    db.add(order)

    purchase = Purchase(
        id=generate_id("pur"), order_id=order_id, occasion_id=occasion.id,
        gift_type=GiftType.GIFT_CARD, brand_name=brand_name,
        hubble_product_id=quote.provider_product_id, amount=quote.amount, status=PurchaseStatus.PENDING,
        personal_message=personal_message, buyer_phone=phone,
        delivery_scheduled_at=occasion.date, delivery_status=DeliveryStatus.PENDING_DELIVERY,
        wishlist_item_id=wishlist_item_id,
    )
    apply_fulfillment_defaults(
        purchase,
        provider_product_id=quote.provider_product_id,
        recipient_user_id=celebrant.id if celebrant else None,
        recipient_phone=celebrant.phone if celebrant else None,
    )
    db.add(purchase)
    await db.flush()

    # Create Razorpay payment link
    try:
        # Razorpay rejects expire_by < now+15min on their side. Computing
        # exactly now+15min races: by the time the request lands, network
        # latency + clock skew tips it under the floor and we get
        # ``BadRequestError: expire_by: timestamp must be atleast 15
        # minutes in future``. 30 minutes gives Razorpay a safe buffer
        # while still being a sensible payment-link expiry.
        expire_by = int(time.time()) + (30 * 60)
        label = occasion.custom_label or occasion.type.value.replace("_", " ").title()
        total = fee_breakdown["total_amount"]
        platform_fee = fee_breakdown["platform_fee"]
        platform_fee_gst = fee_breakdown["platform_fee_gst"]
        fee_total = platform_fee + platform_fee_gst
        rp_result = await razorpay_service.create_payment_link(
            amount=int(total * 100),
            description=f"{brand_name} Gift Card ₹{amount} for {celebrant_name}'s {label} + ₹{fee_total:.2f} gateway fee incl. GST",
            reference_id=order_id,
            customer_name=buyer_name,
            customer_phone=phone,
            expire_by=expire_by,
        )
        link_url = rp_result.get("short_url", "")
        order.razorpay_link_id = rp_result.get("id", "")
        order.razorpay_short_url = link_url
        db.add(order)
        await db.flush()

        return {
            "success": True,
            "order_id": order_id,
            "brand": brand_name,
            "gift_amount": quote.amount,
            "provider": quote.provider,
            "provider_product_id": quote.provider_product_id,
            "platform_fee": float(fee_breakdown["platform_fee"]),
            "platform_fee_gst": float(fee_breakdown["platform_fee_gst"]),
            "total_amount": float(total),
            "fee_label": fee_breakdown["fee_label"],
            "payment_slip": fee_breakdown["payment_slip"],
            "for": celebrant_name,
            "occasion": label,
            "delivery_date": occasion.date.strftime("%d %B"),
            "payment_link": link_url,
            "expires_in": "30 minutes",
            "personal_message": personal_message or "(none)",
        }
    except Exception as e:
        logger.error("Razorpay link creation failed for friend order: %s", e)
        order.status = OrderStatus.FAILED
        purchase.status = PurchaseStatus.FAILED
        db.add(order)
        db.add(purchase)
        await db.flush()
        return {"error": "Could not create payment link. Please try again."}


async def create_batch_gift_orders(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create multiple gift card orders in one batch. Returns a payment link per order.

    items: list of {"brand_name": str, "amount": int, "occasion_id": str | None}
    Used when user wants to handle several occasions in one session.
    """
    items = args.get("items") or []
    if not items:
        return {"error": "items list is required"}
    if len(items) > 10:
        return {"error": "Max 10 orders per batch"}

    results = []
    for item in items:
        sub = await create_gift_order(db, phone, item)
        results.append(sub)

    successes = [r for r in results if r.get("success")]
    failures = [r for r in results if not r.get("success")]

    return {
        "batch_size": len(items),
        "successful": len(successes),
        "failed": len(failures),
        "orders": results,
    }
