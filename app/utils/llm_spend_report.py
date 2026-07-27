"""Admin report for LLM spending and observability."""

from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage_event import LlmUsageEvent


async def get_llm_spend_report(db: AsyncSession) -> dict:
    """Generate LLM spend report for monitoring and cost control.

    Returns metrics for today, month-to-date, by model, by purpose, by phase,
    top spenders, and cache efficiency.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Today's spend
    today_spend = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd)).where(
            LlmUsageEvent.created_at >= today_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    today_spend = float(today_spend or Decimal("0"))

    # Month-to-date spend
    month_spend = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd)).where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    month_spend = float(month_spend or Decimal("0"))

    # Spend by model (month-to-date)
    model_spend_rows = await db.execute(
        select(
            LlmUsageEvent.model,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("total_cost"),
            func.count(LlmUsageEvent.id).label("call_count"),
        )
        .where(LlmUsageEvent.created_at >= month_start)
        .group_by(LlmUsageEvent.model)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
    )
    spend_by_model = {
        row[0]: {"cost": float(row[1] or 0), "calls": row[2]}
        for row in model_spend_rows.all()
    }

    # Spend by purpose (month-to-date)
    purpose_spend_rows = await db.execute(
        select(
            LlmUsageEvent.purpose,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("total_cost"),
            func.count(LlmUsageEvent.id).label("call_count"),
        )
        .where(LlmUsageEvent.created_at >= month_start)
        .group_by(LlmUsageEvent.purpose)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
    )
    spend_by_purpose = {
        row[0]: {"cost": float(row[1] or 0), "calls": row[2]}
        for row in purpose_spend_rows.all()
    }

    # Spend by phase (month-to-date)
    phase_spend_rows = await db.execute(
        select(
            LlmUsageEvent.phase,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("total_cost"),
            func.count(LlmUsageEvent.id).label("call_count"),
        )
        .where(LlmUsageEvent.created_at >= month_start)
        .group_by(LlmUsageEvent.phase)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
    )
    spend_by_phase = {
        (row[0] or "none"): {"cost": float(row[1] or 0), "calls": row[2]}
        for row in phase_spend_rows.all()
    }

    # Top 10 phone_hashes by spend (month-to-date)
    top_spenders_rows = await db.execute(
        select(
            LlmUsageEvent.phone_hash,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("total_cost"),
            func.count(LlmUsageEvent.id).label("call_count"),
        )
        .where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.phone_hash.isnot(None),
        )
        .group_by(LlmUsageEvent.phone_hash)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
        .limit(10)
    )
    top_spenders = [
        {
            "phone_hash": row[0],
            "cost": float(row[1] or 0),
            "calls": row[2],
        }
        for row in top_spenders_rows.all()
    ]

    # Average tokens (month-to-date)
    avg_tokens = await db.execute(
        select(
            func.avg(LlmUsageEvent.input_tokens).label("avg_input"),
            func.avg(LlmUsageEvent.output_tokens).label("avg_output"),
            func.avg(LlmUsageEvent.cache_hit_tokens).label("avg_cache_hit"),
        ).where(LlmUsageEvent.created_at >= month_start)
    )
    avg_row = avg_tokens.first()
    avg_input = float(avg_row[0]) if avg_row[0] else 0
    avg_output = float(avg_row[1]) if avg_row[1] else 0
    avg_cache_hit = float(avg_row[2]) if avg_row[2] else 0

    # Cache hit ratio (month-to-date)
    total_cache_tokens = await db.scalar(
        select(func.sum(LlmUsageEvent.cache_hit_tokens)).where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.cache_hit_tokens.isnot(None),
        )
    )
    total_input_tokens = await db.scalar(
        select(func.sum(LlmUsageEvent.input_tokens)).where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.input_tokens.isnot(None),
        )
    )
    total_cache_tokens = float(total_cache_tokens or 0)
    total_input_tokens = float(total_input_tokens or 0)
    cache_hit_ratio = (
        (total_cache_tokens / (total_cache_tokens + total_input_tokens))
        if (total_cache_tokens + total_input_tokens) > 0
        else 0.0
    )

    return {
        "timestamp": now.isoformat(),
        "today_spend_usd": today_spend,
        "month_spend_usd": month_spend,
        "spend_by_model": spend_by_model,
        "spend_by_purpose": spend_by_purpose,
        "spend_by_phase": spend_by_phase,
        "top_10_spenders": top_spenders,
        "average_tokens": {
            "input": avg_input,
            "output": avg_output,
            "cache_hit": avg_cache_hit,
        },
        "cache_efficiency": {
            "total_cache_tokens": total_cache_tokens,
            "total_input_tokens": total_input_tokens,
            "cache_hit_ratio": cache_hit_ratio,
        },
    }
