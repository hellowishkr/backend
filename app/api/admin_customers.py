"""Admin customers — read-only listing/detail over existing User + Order data.

No new models: customers are just `User` rows, with order history and spend
aggregated from `Order`. Guarded by the same role check as the other admin
routers.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import require_role
from app.api.admin_orders import serialize_order_summary
from app.db.session import get_db
from app.models.order import Order
from app.models.user import User
from app.models.vendor import Vendor

router = APIRouter(prefix="/admin/customers", tags=["admin-customers"])


def serialize_customer_summary(user: User, order_count: int, total_spend: float) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "email": user.email,
        "city_raw": user.city_raw,
        "is_guest": user.is_guest,
        "order_count": order_count,
        "total_spend": total_spend,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("", dependencies=[Depends(require_role("owner", "ops"))])
async def list_customers(
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    base = select(User)
    if q:
        like = f"%{q}%"
        base = base.where(
            (User.name.ilike(like))
            | (User.phone.ilike(like))
            | (User.email.ilike(like))
            | (User.city_raw.ilike(like))
        )

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    guest_count = (
        await db.execute(
            select(func.count()).select_from(base.where(User.is_guest.is_(True)).subquery())
        )
    ).scalar_one()
    matched_user_ids = base.with_only_columns(User.id).subquery()
    repeat_count = (
        await db.execute(
            select(func.count())
            .select_from(
                select(Order.user_id)
                .where(Order.user_id.in_(select(matched_user_ids.c.id)))
                .group_by(Order.user_id)
                .having(func.count(Order.id) > 1)
                .subquery()
            )
        )
    ).scalar_one()
    stats = {"guest_count": guest_count, "repeat_count": repeat_count}

    stmt = base.order_by(User.created_at.desc()).limit(limit).offset((page - 1) * limit)
    users = (await db.execute(stmt)).scalars().all()
    if not users:
        return {"customers": [], "total": total, "page": page, "limit": limit, **stats}

    user_ids = [u.id for u in users]
    agg_rows = (
        await db.execute(
            select(
                Order.user_id,
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_amount), 0),
            )
            .where(Order.user_id.in_(user_ids))
            .group_by(Order.user_id)
        )
    ).all()
    agg = {row[0]: (row[1], float(row[2])) for row in agg_rows}

    return {
        "customers": [
            serialize_customer_summary(u, *agg.get(u.id, (0, 0.0))) for u in users
        ],
        "total": total,
        "page": page,
        "limit": limit,
        **stats,
    }


@router.get("/{user_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def get_customer(user_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="customer not found")

    orders = (
        await db.execute(
            select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc())
        )
    ).scalars().all()

    vendor_ids = {o.vendor_id for o in orders if o.vendor_id}
    vendor_names: dict[str, str] = {}
    if vendor_ids:
        vendors = (
            await db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids)))
        ).scalars().all()
        vendor_names = {v.id: v.name for v in vendors}

    order_count = len(orders)
    total_spend = float(sum(o.total_amount for o in orders if o.total_amount is not None))

    addresses = []
    seen = set()
    for o in orders:
        key = (o.shipping_line1, o.shipping_pincode)
        if o.shipping_line1 and key not in seen:
            seen.add(key)
            addresses.append(
                {
                    "name": o.shipping_name,
                    "phone": o.shipping_phone,
                    "line1": o.shipping_line1,
                    "line2": o.shipping_line2,
                    "city": o.shipping_city,
                    "state": o.shipping_state,
                    "pincode": o.shipping_pincode,
                }
            )

    return {
        "customer": serialize_customer_summary(user, order_count, total_spend),
        "addresses": addresses,
        "orders": [serialize_order_summary(o, vendor_names.get(o.vendor_id)) for o in orders],
    }
