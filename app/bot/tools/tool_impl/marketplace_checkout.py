"""Provider-neutral marketplace selection and checkout tools."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.sources.base import GiftCandidate, ProviderPolicy
from app.bot.state_utils import get_gift_need_profile, get_state, update_active_gift_context
from app.bot.tools.tool_impl.helpers import _HINT_REGISTER, _err, _get_user
from app.models import GiftType, Order, OrderStatus, Purchase, PurchaseStatus, Vendor
from app.models.gift_catalog import GiftCatalogItem
from app.utils.id_gen import generate_id


async def _resolve_marketplace_candidate(
    db: AsyncSession,
    provider_id: str,
    candidate_id: str,
) -> GiftCandidate | None:
    provider_id = (provider_id or "").strip()
    candidate_id = (candidate_id or "").strip()
    if provider_id == "hubble":
        item = await db.get(GiftCatalogItem, candidate_id)
        if item is None:
            from sqlalchemy import select

            item = await db.scalar(
                select(GiftCatalogItem).where(
                    GiftCatalogItem.partner == "hubble",
                    GiftCatalogItem.external_id == candidate_id,
                    GiftCatalogItem.active.is_(True),
                )
            )
        if not item:
            return None
        return GiftCandidate(
            source_id="hubble",
            item_id=item.external_id,
            name=item.brand_name,
            vibes=[],
            price_min=item.min_amount or 0,
            price_max=item.max_amount or 0,
            fulfillment=item.fulfillment_type or "instant_voucher",
            metadata={"category": item.category or "", "is_marketplace": item.is_marketplace},
            policy=ProviderPolicy(
                fulfillment_mode=item.fulfillment_mode or "api",
                delivery_policy=item.delivery_policy,
                return_policy=item.return_policy,
                cancellation_policy=item.cancellation_policy,
                lead_time_hours=item.delivery_lead_time_hours,
                provider_health=item.provider_health or "healthy",
                reliability_score=float(item.reliability_score or 0.8),
            ),
            image_url=item.image_url,
        )
    if provider_id == "vendor_db":
        vendor = await db.get(Vendor, candidate_id)
        if not vendor or not vendor.active:
            return None
        return GiftCandidate(
            source_id="vendor_db",
            item_id=vendor.id,
            name=vendor.name,
            vibes=[],
            price_min=vendor.min_price or 0,
            price_max=vendor.max_price or 0,
            fulfillment="physical_delivery",
            metadata={"category": vendor.category or "", "is_national": vendor.is_national},
            policy=ProviderPolicy(
                fulfillment_mode=vendor.fulfillment_mode or "manual",
                delivery_policy=vendor.delivery_policy,
                return_policy=vendor.return_policy,
                cancellation_policy=vendor.cancellation_policy,
                lead_time_hours=vendor.delivery_lead_time_hours,
                same_day_cutoff_hour=vendor.same_day_cutoff_hour,
                provider_health="healthy",
                reliability_score=float(vendor.reliability_score or 0.5),
            ),
            contact_info=vendor.contact_info,
        )
    return None


async def select_gift_candidate(db: AsyncSession, phone: str, args: dict) -> dict:
    """Persist the user's chosen marketplace candidate in active gift context."""
    provider_id = (args.get("provider_id") or "").strip()
    candidate_id = (args.get("candidate_id") or "").strip()
    if not provider_id or not candidate_id:
        return {
            "error": "missing_candidate",
            "user_message": "Please choose one of the gift options first.",
            "internal_reason": "provider_id_or_candidate_id_missing",
        }
    candidate = await _resolve_marketplace_candidate(db, provider_id, candidate_id)
    if not candidate:
        return {
            "error": "candidate_not_found",
            "user_message": "I could not find that gift option anymore. Please ask me for fresh options.",
            "internal_reason": "marketplace_candidate_not_found",
        }

    await update_active_gift_context(
        db,
        phone,
        {
            "selected_provider_id": provider_id,
            "selected_candidate_id": candidate.item_id,
            "selected_brand": candidate.name,
            "current_step": "candidate_selected",
        },
    )
    return {
        "success": True,
        "provider_id": provider_id,
        "candidate_id": candidate.item_id,
        "name": candidate.name,
        "fulfillment_mode": candidate.policy.fulfillment_mode,
        "price_min": candidate.price_min,
        "price_max": candidate.price_max,
        "message": f"Selected {candidate.name}.",
    }


async def create_checkout(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create checkout for the selected marketplace candidate."""
    profile = get_gift_need_profile(await get_state(db, phone))
    provider_id = (args.get("provider_id") or "").strip() or (profile.selected_provider_id if profile else None)
    candidate_id = (args.get("candidate_id") or "").strip() or (profile.selected_candidate_id if profile else None)
    try:
        amount = int(args.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0

    if not provider_id or not candidate_id:
        return {
            "error": "candidate_not_selected",
            "user_message": "Please pick one gift option before checkout.",
            "internal_reason": "selected_candidate_missing",
        }
    if amount <= 0:
        return {
            "error": "amount_required",
            "user_message": "Please tell me the amount for this gift.",
            "internal_reason": "checkout_amount_missing",
        }
    candidate = await _resolve_marketplace_candidate(db, provider_id, candidate_id)
    if not candidate:
        return {
            "error": "candidate_not_found",
            "user_message": "I could not find that gift option anymore. Please ask me for fresh options.",
            "internal_reason": "marketplace_candidate_not_found",
        }

    if provider_id == "hubble":
        from app.bot.tools.tool_impl.orders import create_friend_gift_order, create_gift_order

        checkout_args = {
            "brand_name": candidate.name,
            "amount": amount,
            "duplicate_acknowledged": bool(args.get("duplicate_acknowledged")),
        }
        if args.get("wishlist_item_id"):
            checkout_args["wishlist_item_id"] = args["wishlist_item_id"]
        if args.get("celebrant_occasion_id"):
            checkout_args["celebrant_occasion_id"] = args["celebrant_occasion_id"]
            checkout_args["buyer_name"] = args.get("buyer_name") or "Friend"
            return await create_friend_gift_order(db, phone, checkout_args)
        if args.get("occasion_id"):
            checkout_args["occasion_id"] = args["occasion_id"]
        return await create_gift_order(db, phone, checkout_args)

    if provider_id == "vendor_db":
        user = await _get_user(db, phone)
        if not user:
            return _err("user_not_registered", _HINT_REGISTER)
        order = Order(
            id=generate_id("ord"),
            user_id=user.id,
            gift_amount=amount,
            platform_fee=0,
            platform_fee_gst=0,
            total_amount=amount,
            status=OrderStatus.NEEDS_MANUAL_REVIEW,
        )
        purchase = Purchase(
            id=generate_id("pur"),
            order_id=order.id,
            occasion_id=(args.get("occasion_id") or "").strip() or None,
            gift_type=GiftType.OTHER,
            brand_name=candidate.name,
            provider=provider_id,
            provider_product_id=candidate.item_id,
            amount=amount,
            status=PurchaseStatus.PENDING,
            special_instructions=(args.get("special_instructions") or "").strip() or None,
            fulfillment_policy="manual_review",
            fulfillment_metadata_json={
                "contact_info": candidate.contact_info,
                "category": candidate.metadata.get("category"),
                "delivery_policy": candidate.policy.delivery_policy,
                "return_policy": candidate.policy.return_policy,
                "cancellation_policy": candidate.policy.cancellation_policy,
            },
        )
        db.add(order)
        db.add(purchase)
        await db.flush()
        return {
            "success": True,
            "manual_review": True,
            "order_id": order.id,
            "purchase_id": purchase.id,
            "provider": provider_id,
            "provider_product_id": candidate.item_id,
            "name": candidate.name,
            "amount": amount,
            "message": "I've sent this gift option for Wishkr manual confirmation.",
        }

    return {
        "error": "provider_not_supported",
        "user_message": "This gift option cannot be checked out yet.",
        "internal_reason": f"unsupported_provider:{provider_id}",
    }
