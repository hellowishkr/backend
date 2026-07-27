"""Admin AI assistant usage — which users are talking to the WhatsApp AI agent.

Read-only aggregation over `ConversationMessage` (full per-message history,
keyed by raw phone — joins directly to `User.phone`). Cost/token figures are
sourced separately from `LlmUsageEvent`, which only stores a one-way hash of
the phone number, so they're matched by hashing each candidate user's phone.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import require_role
from app.db.session import get_db
from app.models.conversation_message import ConversationMessage
from app.models.conversation import ConversationState
from app.models.llm_usage_event import LlmUsageEvent
from app.models.user import User
from app.utils.phone_hash import hash_phone

router = APIRouter(prefix="/admin/ai-usage", tags=["admin-ai-usage"])


def _phone_candidates(phone: str) -> list[str]:
    digits = "".join(c for c in phone if c.isdigit())
    candidates = [phone]
    if digits and digits != phone:
        candidates.append(digits)
    if len(digits) == 10:
        candidates.append(f"91{digits}")
    return list(dict.fromkeys(candidates))


@router.get("", dependencies=[Depends(require_role("owner", "ops"))])
async def list_ai_assistant_users(
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    agg = (
        select(
            ConversationMessage.phone,
            func.count(ConversationMessage.id).label("message_count"),
            func.max(ConversationMessage.created_at).label("last_active_at"),
            func.min(ConversationMessage.created_at).label("first_active_at"),
        )
        .group_by(ConversationMessage.phone)
        .subquery()
    )

    stmt = (
        select(agg.c.phone, User, agg.c.message_count, agg.c.last_active_at, agg.c.first_active_at)
        .select_from(agg)
        .outerjoin(User, agg.c.phone == User.phone)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(agg.c.phone.ilike(like), User.name.ilike(like), User.phone.ilike(like))
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    stmt = stmt.order_by(agg.c.last_active_at.desc()).limit(limit).offset((page - 1) * limit)
    rows = (await db.execute(stmt)).all()

    flows = {}
    if rows:
        phones = [phone for phone, _, _, _, _ in rows]
        flow_rows = (
            await db.execute(select(ConversationState).where(ConversationState.phone.in_(phones)))
        ).scalars().all()
        flows = {cs.phone: cs.flow.value for cs in flow_rows}

    return {
        "users": [
            {
                "user_id": user.id if user else phone,
                "name": user.name if user else None,
                "phone": phone,
                "message_count": message_count,
                "first_active_at": first_active_at.isoformat() if first_active_at else None,
                "last_active_at": last_active_at.isoformat() if last_active_at else None,
                "current_flow": flows.get(phone),
            }
            for phone, user, message_count, last_active_at, first_active_at in rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/{user_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def get_ai_assistant_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    message_limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    user = await db.get(User, user_id)
    candidates = _phone_candidates(user.phone) if user else [user_id]

    counts = (
        await db.execute(
            select(ConversationMessage.phone, func.count(ConversationMessage.id))
            .where(ConversationMessage.phone.in_(candidates))
            .group_by(ConversationMessage.phone)
        )
    ).all()
    count_by_phone = {phone: count for phone, count in counts}
    phone = next((candidate for candidate in candidates if count_by_phone.get(candidate, 0) > 0), None)

    if user is None and phone is None:
        raise HTTPException(status_code=404, detail="user not found")
    if phone is None:
        phone = candidates[0]
    message_count = count_by_phone.get(phone, 0)

    messages = (
        await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.phone == phone)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(message_limit)
        )
    ).scalars().all()

    usage_totals = (
        await db.execute(
            select(
                func.count(LlmUsageEvent.id),
                func.coalesce(func.sum(LlmUsageEvent.total_tokens), 0),
                func.coalesce(func.sum(LlmUsageEvent.estimated_cost_usd), 0),
            ).where(LlmUsageEvent.phone_hash == hash_phone(phone))
        )
    ).one()
    call_count, total_tokens, total_cost_usd = usage_totals

    return {
        "user": {
            "id": user.id if user else phone,
            "name": user.name if user else None,
            "phone": phone,
        },
        "message_count": message_count,
        "usage": {
            "llm_call_count": call_count,
            "total_tokens": int(total_tokens),
            "total_cost_usd": float(total_cost_usd),
        },
        "messages": [
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "tool_name": m.tool_name,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in reversed(messages)
        ],
    }
