"""Owned-order facts for the conversational agent; no language interpretation."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Order, User
from app.utils.phones import phone_aliases


async def get_order_support(db: AsyncSession, phone: str, args: dict) -> dict:
    """Look up order facts using the trusted caller identity, including guests."""
    order_id = (args.get("order_id") or "").strip()
    query = (
        select(Order)
        .join(User, User.id == Order.user_id)
        .where(User.phone.in_(phone_aliases(phone)))
    )
    if order_id:
        query = query.where(Order.id == order_id)
    orders = list(await db.scalars(query.order_by(Order.created_at.desc()).limit(5)))
    return {
        "orders": [
            {
                "order_id": order.id,
                "status": order.status.value,
                "fulfillment_status": order.fulfillment_status,
                "tracking_url": order.tracking_url,
            }
            for order in orders
        ],
        "order_verified": bool(orders),
        "support_email": settings.order_notification_email_to or "hello@wishkr.com",
        "return_eligibility": "requires_team_review",
        "order_changed": False,
        "review_started": False,
        "team_notified": False,
        "guidance": "Only describe returned facts. Empty results do not verify ownership. Return/cancellation requests require team review; never claim approval or that support was contacted.",
    }
