"""Admin orders — read + manage (Phase 2 of the admin panel).

Read-only listing/detail plus admin actions: edit line items, create a top-up
payment link for a price increase, update fulfilment/tracking, resend the
vendor WhatsApp notification, and read the change-history audit trail.
Guarded by the same X-Admin-Token as the catalog router.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import require_role
from app.api.product_orders import (
    ProductOrderItemPayload,
    compute_order_shipping_paise,
    price_line,
)
from app.db.session import get_db
from app.models import User
from app.models.admin_user import AuditLog
from app.models.order import Order, OrderStatus
from app.models.order_line_item import OrderLineItem
from app.models.order_payment_link import OrderPaymentLink
from app.models.product import Product
from app.models.vendor import Vendor
from app.services.razorpay_service import razorpay_service
from app.utils.email import send_email
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.admin_orders")

router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


def _paise(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def serialize_order_summary(order: Order, vendor_name: str | None) -> dict:
    return {
        "id": order.id,
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "order_type": order.order_type,
        "fulfillment_status": getattr(order, "fulfillment_status", "pending"),
        "tracking_number": order.tracking_number,
        "tracking_carrier": order.tracking_carrier,
        "tracking_url": order.tracking_url,
        "total_amount": float(order.total_amount) if order.total_amount is not None else None,
        "vendor_id": order.vendor_id,
        "vendor_name": vendor_name,
        "vendor_amount_paise": order.vendor_amount_paise,
        "platform_amount_paise": order.platform_amount_paise,
        "vendor_notified_at": (
            order.vendor_notified_at.isoformat()
            if getattr(order, "vendor_notified_at", None)
            else None
        ),
        "customer_name": order.shipping_name,
        "customer_phone": order.shipping_phone,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "marketing_attribution": order.marketing_attribution,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def serialize_order_detail(
    order: Order,
    vendor_name: str | None,
    line_items: list[OrderLineItem],
    product_names: dict[str, str],
    payment_links: list[OrderPaymentLink] | None = None,
    outstanding_paise: int = 0,
) -> dict:
    base = serialize_order_summary(order, vendor_name)
    base.update(
        {
            "shipping": {
                "name": order.shipping_name,
                "phone": order.shipping_phone,
                "line1": order.shipping_line1,
                "line2": order.shipping_line2,
                "city": order.shipping_city,
                "state": order.shipping_state,
                "pincode": order.shipping_pincode,
            },
            "gift_message": order.gift_message,
            "hide_prices": order.hide_prices,
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_payment_id": order.razorpay_payment_id,
            "amount_collected_paise": order.amount_collected_paise,
            "outstanding_paise": outstanding_paise,
            "payment_links": [
                {
                    "id": pl.id,
                    "amount_paise": pl.amount_paise,
                    "reason": pl.reason,
                    "status": pl.status,
                    "short_url": pl.razorpay_short_url,
                    "created_at": pl.created_at.isoformat() if pl.created_at else None,
                    "paid_at": pl.paid_at.isoformat() if pl.paid_at else None,
                }
                for pl in (payment_links or [])
            ],
            "line_items": [
                {
                    "id": li.id,
                    "product_id": li.product_id,
                    "product_name": product_names.get(li.product_id, li.product_id),
                    "variant_id": li.variant_id,
                    "selected_addons": li.selected_addons or [],
                    "customizations": li.customizations or {},
                    "qty": li.qty,
                    "unit_price_paise": li.unit_price_paise,
                    "line_total_paise": li.line_total_paise,
                    "shipping_fee_paise": li.shipping_fee_paise,
                }
                for li in line_items
            ],
        }
    )
    return base


async def _outstanding_amount_paise(db: AsyncSession, order: Order) -> int:
    """Amount still owed: live total_amount minus what's actually been
    collected (the frozen original payment + any paid top-up links).
    amount_collected_paise is NULL for orders paid before this feature
    shipped — treated as "the full current total_amount was collected",
    which is exact for any order that hasn't been edited yet."""
    paid_topups = (
        await db.execute(
            select(OrderPaymentLink).where(
                OrderPaymentLink.order_id == order.id, OrderPaymentLink.status == "paid"
            )
        )
    ).scalars().all()
    total_paise = int(round((order.total_amount or Decimal(0)) * 100))
    collected = order.amount_collected_paise
    if collected is None:
        collected = total_paise
    collected += sum(pl.amount_paise for pl in paid_topups)
    return max(0, total_paise - collected)


@router.get("", dependencies=[Depends(require_role("owner", "ops"))])
async def list_orders(
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None),
    fulfillment: str | None = Query(default=None),
    vendor_id: str | None = Query(default=None),
    order_type: str | None = Query(default="product"),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    stmt = select(Order).order_by(Order.created_at.desc())
    if order_type:
        stmt = stmt.where(Order.order_type == order_type)
    if vendor_id:
        stmt = stmt.where(Order.vendor_id == vendor_id)
    if fulfillment:
        stmt = stmt.where(Order.fulfillment_status == fulfillment)
    orders = (await db.execute(stmt)).scalars().all()

    # status filter (enum) applied in Python to tolerate string/enum mismatch.
    if status:
        orders = [
            o for o in orders
            if (o.status.value if hasattr(o.status, "value") else str(o.status)) == status
        ]

    vendor_ids = {o.vendor_id for o in orders if o.vendor_id}
    vendor_names: dict[str, str] = {}
    if vendor_ids:
        vendors = (
            await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
        ).scalars().all()
        vendor_names = {v.id: v.name for v in vendors}

    if q:
        needle = q.lower()
        orders = [
            o for o in orders
            if needle
            in f"{o.id} {o.shipping_name or ''} {vendor_names.get(o.vendor_id, '')}".lower()
        ]

    total = len(orders)
    orders = orders[(page - 1) * limit : (page - 1) * limit + limit]

    return {
        "orders": [serialize_order_summary(o, vendor_names.get(o.vendor_id)) for o in orders],
        "total": total,
        "page": page,
        "limit": limit,
    }


async def _get_order_or_404(db: AsyncSession, order_id: str) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@router.get("/{order_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await _get_order_or_404(db, order_id)
    line_items = (
        await db.execute(select(OrderLineItem).where(OrderLineItem.order_id == order.id))
    ).scalars().all()
    vendor_name = None
    if order.vendor_id:
        vendor = await db.get(Vendor, order.vendor_id)
        vendor_name = vendor.name if vendor else None
    product_ids = {li.product_id for li in line_items}
    product_names: dict[str, str] = {}
    if product_ids:
        products = (
            await db.execute(select(Product).where(Product.id.in_(product_ids)))
        ).scalars().all()
        product_names = {p.id: p.name for p in products}
    payment_links = (
        await db.execute(
            select(OrderPaymentLink)
            .where(OrderPaymentLink.order_id == order.id)
            .order_by(OrderPaymentLink.created_at.desc())
        )
    ).scalars().all()
    outstanding_paise = await _outstanding_amount_paise(db, order)
    return {
        "order": serialize_order_detail(
            order, vendor_name, list(line_items), product_names,
            payment_links=list(payment_links), outstanding_paise=outstanding_paise,
        )
    }


async def _cancel_active_link_if_any(
    db: AsyncSession, order_id: str, x_admin_email: str | None
) -> None:
    """Move any creating/created top-up link for this order through
    cancel_pending -> cancelled. Called after an edit changes the order total
    (any open link was priced against the old total) and before minting a
    fresh link (never leave two active links for one order).

    cancel_pending is committed FIRST, separately from the cancelled
    transition, so a crash between the two leaves a reconcilable row instead
    of one that looks untouched. A Razorpay cancel failure is logged and left
    in cancel_pending for app.jobs.topup_link_reconciliation to retry — it
    does NOT raise, so it never blocks the caller (an edit save, or a fresh
    top-up-link request) on an unrelated Razorpay hiccup.
    """
    active = (
        await db.execute(
            select(OrderPaymentLink).where(
                OrderPaymentLink.order_id == order_id,
                OrderPaymentLink.status.in_(["creating", "created"]),
            )
        )
    ).scalars().all()
    if not active:
        return

    now = datetime.now(timezone.utc)
    for link in active:
        link.status = "cancel_pending"
        link.state_changed_at = now
    await db.commit()

    for link in active:
        if link.razorpay_link_id:
            try:
                await razorpay_service.cancel_payment_link(link.razorpay_link_id)
            except Exception as exc:  # noqa: BLE001 — reconciliation job retries this
                logger.warning(
                    "cancel_payment_link failed for %s (order=%s), left cancel_pending "
                    "for reconciliation: %s",
                    link.razorpay_link_id, order_id, exc,
                )
                continue
        link.status = "cancelled"
        link.state_changed_at = datetime.now(timezone.utc)
    await db.commit()


async def _advance_creating_link(link_id: str) -> None:
    """Call Razorpay for a 'creating' row and land it on 'created' or 'failed'.

    Three phases, each its own short DB session — no session/connection is
    ever held open during the Razorpay HTTP call, and the phase-1 claim is
    what actually prevents two concurrent callers (the fast path and the
    reconciliation job) from both calling Razorpay for the same row:

    1. Claim: UPDATE ... WHERE status='creating' -> 'claiming'. Only one
       concurrent caller's UPDATE can match the row (rowcount 1); every
       other caller sees rowcount 0 and returns immediately. This is a
       compare-and-swap, not a lock — no session stays open past the commit.
    2. Call Razorpay with NO db session open.
    3. Persist the result ('created' or 'failed') in a fresh short session.

    Called both fire-and-forget from create_topup_link (fast path, ~1-2s
    typical) and from app.jobs.topup_link_reconciliation (recovery path for
    rows stuck in 'creating' or 'claiming' — the latter meaning a crash
    happened between phase 1 and phase 3 of a previous attempt).
    """
    from app.db.session import async_session

    # Phase 1: claim.
    async with async_session() as db:
        claim = await db.execute(
            update(OrderPaymentLink)
            .where(OrderPaymentLink.id == link_id, OrderPaymentLink.status == "creating")
            .values(status="claiming", state_changed_at=datetime.now(timezone.utc))
        )
        await db.commit()
        if claim.rowcount == 0:
            return  # already claimed by a concurrent caller, or not in 'creating'

        opl = await db.get(OrderPaymentLink, link_id)
        order = await db.get(Order, opl.order_id) if opl else None
        if opl is None:
            return
        if order is None:
            opl.status = "failed"
            opl.last_error = "order not found"
            opl.state_changed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        amount_paise, reason = opl.amount_paise, opl.reason
        user = await db.get(User, order.user_id)
        customer_name = (user.name if user else order.shipping_name) or "Customer"
        customer_phone = (user.phone if user else order.shipping_phone) or ""
        customer_email = (user.email if user else None) or ""
        order_id = order.id

    # Phase 2: call Razorpay — no db session/connection open during this call.
    razorpay_link: dict | None = None
    try:
        razorpay_link = await razorpay_service.create_payment_link(
            amount=amount_paise,
            description=(
                f"Additional amount for order {order_id}" + (f" — {reason}" if reason else "")
            ),
            reference_id=link_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        result_status, result_error = "created", None
    except Exception as exc:  # noqa: BLE001 — reconciliation job retries this
        result_status, result_error = "failed", str(exc)[:500]
        logger.warning(
            "topup link create failed link=%s order=%s: %s", link_id, order_id, exc,
        )

    # Phase 3: persist the result in a fresh short session.
    async with async_session() as db:
        opl = await db.get(OrderPaymentLink, link_id)
        if opl is None or opl.status != "claiming":
            return  # defensive — the claim in phase 1 was exclusive
        if result_status == "created" and razorpay_link is not None:
            opl.razorpay_link_id = razorpay_link.get("id")
            opl.razorpay_short_url = razorpay_link.get("short_url")
            opl.status = "created"
        else:
            opl.status = "failed"
            opl.retry_count = (opl.retry_count or 0) + 1
            opl.last_error = result_error
        opl.state_changed_at = datetime.now(timezone.utc)
        await db.commit()

    # Best-effort — never blocks the state transition above.
    if result_status == "created" and razorpay_link and customer_email and razorpay_link.get("short_url"):
        try:
            await send_email(
                to_email=customer_email,
                subject=f"Additional payment needed for your Wishkr order #{order_id}",
                text=(
                    f"Hi {customer_name},\n\n"
                    f"Your order #{order_id} was updated"
                    + (f" ({reason})" if reason else "")
                    + ".\n"
                    f"An additional ₹{amount_paise / 100:.2f} is due.\n\n"
                    f"Pay securely here: {razorpay_link.get('short_url')}\n\n"
                    "— Team Wishkr"
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Top-up link email failed for order=%s: %s", order_id, e)


class EditOrderItemsPayload(BaseModel):
    items: list[ProductOrderItemPayload] = Field(min_length=1, max_length=20)


@router.put("/{order_id}/items", dependencies=[Depends(require_role("owner", "ops"))])
async def edit_order_items(
    order_id: str,
    payload: EditOrderItemsPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    from app.api.admin_settings import record_audit

    # The order row lock covers only the edit itself (a genuine read-modify-
    # write on total_amount/vendor_amount_paise/platform_amount_paise) — it
    # is released via commit BELOW, before any top-up-link cancellation,
    # since cancelling a Razorpay link is an external HTTP call and must
    # never happen while holding a DB row lock.
    order = (
        await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.order_type != "product":
        raise HTTPException(status_code=400, detail="only product orders can have their items edited")
    if order.status not in (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.COMPLETED):
        raise HTTPException(
            status_code=400,
            detail=f"order is {order.status.value}; items can only be edited on a paid order",
        )

    old_total = order.total_amount
    # Orders paid before amount_collected_paise existed have it as NULL. The
    # first edit on such an order is the last moment we can capture what was
    # truly collected — freeze it now, from the pre-edit total, or every
    # later outstanding-balance calculation silently drifts against the
    # (about to change) live total_amount.
    if order.amount_collected_paise is None:
        order.amount_collected_paise = int(round((old_total or Decimal(0)) * 100))

    priced_lines = [await price_line(db, item) for item in payload.items]
    vendor_ids = {line.product.vendor_id for line in priced_lines}
    if len(vendor_ids) != 1:
        raise HTTPException(status_code=400, detail="order items must stay within one vendor")
    new_vendor_id = next(iter(vendor_ids))
    if order.vendor_id and new_vendor_id != order.vendor_id:
        raise HTTPException(status_code=400, detail="cannot move an order to a different vendor")

    vendor = priced_lines[0].product.vendor
    product_total_paise = sum(line.line_total_paise for line in priced_lines)
    shipping_total_paise = compute_order_shipping_paise(product_total_paise)
    total_paise = product_total_paise + shipping_total_paise
    platform_amount_paise = round(product_total_paise * 0.10)
    vendor_amount_paise = total_paise - platform_amount_paise

    new_total = Decimal(total_paise) / Decimal(100)
    if new_total < old_total:
        # A price decrease implies money owed back to the customer. This
        # endpoint has no refund/credit flow yet, and _outstanding_amount_paise
        # clamps negative deltas to zero — silently accepting a lower total
        # would leave the customer overcharged with no record of it. Refunds
        # go through the existing Razorpay refund path outside this endpoint.
        raise HTTPException(
            status_code=400,
            detail=(
                f"new total ₹{new_total} is less than the current ₹{old_total}; "
                "this endpoint doesn't support refunds — issue a refund separately, "
                "then edit the order to match"
            ),
        )

    # Replace line items wholesale — cascade="all, delete-orphan" on
    # Order.line_items makes clearing + re-adding safe.
    existing = (
        await db.execute(select(OrderLineItem).where(OrderLineItem.order_id == order.id))
    ).scalars().all()
    for li in existing:
        await db.delete(li)
    await db.flush()

    shipping_line_index = max(
        range(len(priced_lines)), key=lambda i: priced_lines[i].shipping_fee_paise, default=-1,
    )
    for index, line in enumerate(priced_lines):
        db.add(
            OrderLineItem(
                id=generate_id("oli"),
                order_id=order.id,
                vendor_id=line.product.vendor_id,
                product_id=line.product.id,
                variant_id=line.variant.id if line.variant else None,
                product_name=line.product.name,
                product_image_url=line.product.image_url,
                selected_addons=line.selected_addons,
                customizations=line.customizations,
                qty=line.qty,
                unit_price_paise=line.unit_price_paise,
                line_total_paise=line.line_total_paise,
                shipping_fee_paise=(shipping_total_paise if index == shipping_line_index else 0),
            )
        )

    order.total_amount = new_total
    order.vendor_amount_paise = vendor_amount_paise
    order.platform_amount_paise = platform_amount_paise
    if not order.vendor_id:
        order.vendor_id = new_vendor_id

    await record_audit(
        db,
        actor=x_admin_email,
        action="order.items_edited",
        entity_type="order",
        entity_id=order.id,
        summary=f"Total changed ₹{old_total} → ₹{order.total_amount} ({len(priced_lines)} item(s))",
    )
    await db.commit()  # release the FOR UPDATE lock — the edit is durable now

    # Any still-active top-up link was priced against the pre-edit total and
    # is now stale. Done AFTER the edit commits, not inside the locked
    # transaction — cancelling on Razorpay is an external HTTP call.
    await _cancel_active_link_if_any(db, order.id, x_admin_email)

    return await get_order(order_id, db)


class CreateTopupPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


@router.post(
    "/{order_id}/topup-link",
    status_code=202,
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def create_topup_link(
    order_id: str,
    payload: CreateTopupPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    """Start creating a top-up payment link. Returns 202 immediately with
    status='creating' — no Razorpay call has been made yet. The caller should
    poll GET /{order_id} and watch payment_links for this id to reach
    'created' (with a short_url) or 'failed'.

    The order row is locked only for this short check-and-insert — no
    external HTTP call happens while any lock is held. Concurrent requests
    are prevented not by the lock (which is released the moment this
    function returns) but by the partial unique index
    uq_order_active_payment_link, which makes a second concurrent insert for
    the same order fail with IntegrityError, caught below and turned into a
    clean 409.
    """
    from app.api.admin_settings import record_audit

    order = (
        await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.order_type != "product":
        raise HTTPException(status_code=400, detail="only product orders support top-up links")
    if order.status not in (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.COMPLETED):
        raise HTTPException(status_code=400, detail="order has not been paid yet; cannot top up")

    outstanding_paise = await _outstanding_amount_paise(db, order)
    if outstanding_paise <= 0:
        raise HTTPException(status_code=400, detail="no outstanding amount to collect")

    await _cancel_active_link_if_any(db, order.id, x_admin_email)

    link_id = generate_id("opl")
    opl = OrderPaymentLink(
        id=link_id,
        order_id=order.id,
        amount_paise=outstanding_paise,
        reason=payload.reason,
        status="creating",
        created_by=x_admin_email,
    )
    db.add(opl)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # The partial unique index caught a race: another request already
        # has an active (creating/created) link for this order. This is the
        # actual concurrency backstop — _cancel_active_link_if_any above
        # should normally prevent reaching here, but the index is what
        # makes "at most one active link" true regardless of timing.
        raise HTTPException(
            status_code=409,
            detail="a payment link is already being created or is active for this order; refresh and retry",
        )

    await record_audit(
        db,
        actor=x_admin_email,
        action="order.topup_link_creating",
        entity_type="order",
        entity_id=order.id,
        summary=(
            f"Payment link creation started for ₹{outstanding_paise / 100:.2f}"
            + (f" ({payload.reason})" if payload.reason else "")
        ),
    )
    await db.commit()

    # Fast path: attempt the Razorpay call immediately in the background so
    # the admin typically sees 'created' within ~1-2s of polling. This is
    # best-effort only — app.jobs.topup_link_reconciliation is what actually
    # guarantees a stuck 'creating' row (process restart, worker recycle)
    # gets advanced; a lost create_task here is not a correctness bug.
    asyncio.create_task(_advance_creating_link(link_id))

    return {"id": opl.id, "order_id": order.id, "status": "creating", "amount_paise": outstanding_paise}


class FulfillmentPayload(BaseModel):
    fulfillment_status: str = Field(pattern="^(pending|shipped|delivered)$")
    tracking_number: str | None = Field(default=None, max_length=100)
    tracking_carrier: str | None = Field(default=None, max_length=80)
    tracking_url: str | None = Field(default=None, max_length=500)


@router.patch("/{order_id}/fulfillment", dependencies=[Depends(require_role("owner", "ops"))])
async def update_fulfillment(
    order_id: str,
    payload: FulfillmentPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    from app.api.admin_settings import record_audit

    order = await _get_order_or_404(db, order_id)
    old_status = order.fulfillment_status
    old_tracking = (order.tracking_carrier, order.tracking_number, order.tracking_url)

    # Every value here — including None — is the admin's actual intent (the
    # UI always sends the full form on save); None means "clear this field",
    # not "field omitted", so it's applied unconditionally rather than only
    # when not None.
    order.fulfillment_status = payload.fulfillment_status
    order.tracking_number = payload.tracking_number
    order.tracking_carrier = payload.tracking_carrier
    order.tracking_url = payload.tracking_url

    new_tracking = (order.tracking_carrier, order.tracking_number, order.tracking_url)
    if old_status != order.fulfillment_status or old_tracking != new_tracking:
        summary_parts = []
        if old_status != order.fulfillment_status:
            summary_parts.append(f"Fulfillment {old_status} → {order.fulfillment_status}")
        if old_tracking != new_tracking:
            summary_parts.append("tracking details updated")
        await record_audit(
            db,
            actor=x_admin_email,
            action="order.fulfillment_updated",
            entity_type="order",
            entity_id=order.id,
            summary="; ".join(summary_parts),
        )
    await db.commit()
    return {
        "ok": True,
        "fulfillment_status": order.fulfillment_status,
        "tracking_number": order.tracking_number,
        "tracking_carrier": order.tracking_carrier,
        "tracking_url": order.tracking_url,
    }


@router.get("/{order_id}/history", dependencies=[Depends(require_role("owner", "ops"))])
async def get_order_history(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "order", AuditLog.entity_id == order_id)
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    return {
        "history": [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "summary": r.summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post(
    "/{order_id}/resend-vendor-notification",
    dependencies=[Depends(require_role("owner", "ops"))],
)
async def resend_vendor_notification(order_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    order = await _get_order_or_404(db, order_id)
    # Reuse the exact webhook notification builder so the message format matches.
    from app.webhooks.razorpay_handlers import _notify_vendor_of_product_order

    try:
        await _notify_vendor_of_product_order(order, db)
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the admin UI
        raise HTTPException(
            status_code=502, detail=f"Vendor notification failed: {str(exc)[:200]}"
        ) from exc
    order.vendor_notified_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "vendor_notified_at": order.vendor_notified_at.isoformat()}
