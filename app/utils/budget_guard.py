"""Budget guard for LLM spending — prevents runaway costs.

Checks global daily/monthly and per-user daily budgets before LLM calls.
Requires database access to sum recent spending.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.llm_usage_event import LlmUsageEvent


async def get_daily_budget_usage(db: AsyncSession) -> tuple[Decimal, Decimal, float]:
    """Return (spent_today_usd, daily_budget_usd, usage_pct)."""
    budget = Decimal(str(settings.llm_daily_budget_usd))
    if budget <= 0:
        return Decimal("0"), Decimal("0"), 0.0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd))
        .where(
            LlmUsageEvent.created_at >= today_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    spent = result or Decimal("0")
    usage_pct = float((spent / budget) * Decimal("100")) if budget > 0 else 0.0
    return spent, budget, usage_pct


async def check_global_daily_budget(db: AsyncSession) -> bool:
    """Return True if we can make another LLM call (global daily budget not exceeded)."""
    if settings.llm_daily_budget_usd <= 0:
        return True  # Budget disabled

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd))
        .where(
            LlmUsageEvent.created_at >= today_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    spent_today = result or Decimal("0")
    return spent_today < Decimal(str(settings.llm_daily_budget_usd))


async def check_global_monthly_budget(db: AsyncSession) -> bool:
    """Return True if we can make another LLM call (global monthly budget not exceeded)."""
    if settings.llm_monthly_budget_usd <= 0:
        return True  # Budget disabled

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd))
        .where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    spent_month = result or Decimal("0")
    return spent_month < Decimal(str(settings.llm_monthly_budget_usd))


async def check_per_user_daily_budget(db: AsyncSession, phone_hash: str) -> bool:
    """Return True if this phone can make another LLM call (per-user daily limit not exceeded)."""
    if settings.llm_per_user_daily_budget_usd <= 0:
        return True  # Budget disabled

    if not phone_hash:
        return True  # No phone hash, cannot check

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd))
        .where(
            LlmUsageEvent.phone_hash == phone_hash,
            LlmUsageEvent.created_at >= today_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    spent_today = result or Decimal("0")
    return spent_today < Decimal(str(settings.llm_per_user_daily_budget_usd))


async def check_all_budgets(db: AsyncSession, phone_hash: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Check all applicable budgets. Return (allowed, violation_reason).

    If allowed is False, reason explains which budget was exceeded:
    - 'global_daily'
    - 'global_monthly'
    - 'per_user_daily'
    """
    if not await check_global_daily_budget(db):
        return False, "global_daily"
    if not await check_global_monthly_budget(db):
        return False, "global_monthly"
    if phone_hash and not await check_per_user_daily_budget(db, phone_hash):
        return False, "per_user_daily"
    return True, None
