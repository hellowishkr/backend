import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationState
from app.bot.state_utils import get_state, has_active_gift_context, update_active_gift_context
from app.bot.tools.tool_impl.helpers import (
    _err, _HINT_OCCASION_UNKNOWN,
    _get_user, _user_owns_occasion,
)

CART_KEY = "cart_items"
CART_CREATED_AT_KEY = "cart_created_at"
CART_TTL_HOURS = 24


async def _get_conv_state(db: AsyncSession, phone: str) -> tuple[dict, object]:
    """Load conversation state + parsed temp_data dict. Creates state row if missing."""
    result = await db.execute(
        select(ConversationState).where(ConversationState.phone == phone)
    )
    state = result.scalar_one_or_none()
    if not state:
        state = ConversationState(phone=phone)
        db.add(state)
        await db.flush()

    temp: dict = {}
    if state.temp_data:
        try:
            parsed = json.loads(state.temp_data)
            if isinstance(parsed, dict):
                temp = parsed
        except json.JSONDecodeError:
            temp = {}
    return temp, state


async def _save_conv_state(db: AsyncSession, state, temp: dict) -> None:
    state.temp_data = json.dumps(temp)
    db.add(state)
    await db.flush()


def _parse_cart_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cart_expired(temp: dict, now: datetime | None = None) -> bool:
    cart = temp.get(CART_KEY) or []
    if not cart:
        return False
    created_at = _parse_cart_created_at(temp.get(CART_CREATED_AT_KEY))
    if created_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return created_at < now - timedelta(hours=CART_TTL_HOURS)


def _clear_cart_temp(temp: dict) -> None:
    temp[CART_KEY] = []
    temp.pop(CART_CREATED_AT_KEY, None)


async def _load_active_cart(db: AsyncSession, phone: str) -> tuple[list, dict, object, bool]:
    temp, state = await _get_conv_state(db, phone)
    if _cart_expired(temp):
        _clear_cart_temp(temp)
        await _save_conv_state(db, state, temp)
        return [], temp, state, True
    cart = temp.get(CART_KEY) or []
    return cart if isinstance(cart, list) else [], temp, state, False


async def add_to_cart(db: AsyncSession, phone: str, args: dict) -> dict:
    """Add a gift card item to the user's 24-hour session cart."""
    brand_name = (args.get("brand_name") or "").strip()
    try:
        amount = int(args.get("amount") or 0)
    except (TypeError, ValueError):
        return {"error": "amount must be an integer"}
    occasion_id = (args.get("occasion_id") or "").strip() or None

    if not brand_name or amount <= 0:
        return {"error": "brand_name and positive amount are required"}

    # If the LLM tagged this cart line to an occasion, the caller must own it.
    # Otherwise an attacker could probe for occasion existence and silently
    # cross-link a future checkout to someone else's occasion row.
    if occasion_id:
        user = await _get_user(db, phone)
        if not user or not await _user_owns_occasion(db, occasion_id, user.id):
            return _err("occasion_not_found", _HINT_OCCASION_UNKNOWN, occasion_id=occasion_id)

    cart, temp, state, expired = await _load_active_cart(db, phone)
    if len(cart) >= 10:
        return {"error": "Cart is full (max 10 items). Checkout or remove items first."}

    cart.append({
        "brand_name": brand_name,
        "amount": amount,
        "occasion_id": occasion_id,
    })
    temp[CART_KEY] = cart
    temp[CART_CREATED_AT_KEY] = temp.get(CART_CREATED_AT_KEY) or datetime.now(timezone.utc).isoformat()
    await _save_conv_state(db, state, temp)

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

    return {
        "success": True,
        "cart_size": len(cart),
        "cart_ttl_hours": CART_TTL_HOURS,
        "expired_previous_cart": expired,
        "message": f"Added {brand_name} ₹{amount} to cart. ({len(cart)} item{'s' if len(cart) != 1 else ''})",
    }


async def view_cart(db: AsyncSession, phone: str, args: dict) -> dict:
    """Show the user's current unexpired cart items and total."""
    cart, _temp, _state, expired = await _load_active_cart(db, phone)

    if not cart:
        message = "Cart expired after 24 hours. Add items again to continue." if expired else "Cart is empty."
        return {"cart": [], "total": 0, "expired": expired, "message": message}

    total = sum(int(item.get("amount", 0)) for item in cart)
    return {
        "cart": cart,
        "count": len(cart),
        "total": total,
        "cart_ttl_hours": CART_TTL_HOURS,
    }


async def checkout_cart(db: AsyncSession, phone: str, args: dict) -> dict:
    """Create orders for all unexpired cart items and clear the cart."""
    from app.bot.tools.tool_impl.orders import create_batch_gift_orders
    cart, temp, state, expired = await _load_active_cart(db, phone)

    if not cart:
        if expired:
            return {"error": "Cart expired after 24 hours. Add items again before checkout."}
        return {"error": "Cart is empty — add items first."}

    result = await create_batch_gift_orders(db, phone, {"items": cart})

    # Clear cart on successful checkout
    _clear_cart_temp(temp)
    await _save_conv_state(db, state, temp)

    return result


async def clear_cart(db: AsyncSession, phone: str, args: dict) -> dict:
    """Empty the user's cart without creating orders."""
    temp, state = await _get_conv_state(db, phone)
    had = len(temp.get(CART_KEY) or [])
    _clear_cart_temp(temp)
    await _save_conv_state(db, state, temp)
    return {
        "success": True,
        "message": f"Cleared {had} item{'s' if had != 1 else ''} from cart." if had else "Cart was already empty.",
    }
