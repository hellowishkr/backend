"""Founder-facing daily LLM cost report and anomaly detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.llm_usage_event import LlmUsageEvent


async def detect_expensive_conversations(db: AsyncSession, *, lookback_days: int = 1) -> list[dict]:
    """Flag expensive conversations by cost, tool-call volume, and cache ratio."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    rows = await db.execute(
        select(
            LlmUsageEvent.conversation_id,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("cost"),
            func.sum(func.coalesce(LlmUsageEvent.tool_calls_count, 0)).label("tool_calls"),
            func.sum(func.coalesce(LlmUsageEvent.cache_hit_tokens, 0)).label("cache_tokens"),
            func.sum(func.coalesce(LlmUsageEvent.input_tokens, 0)).label("input_tokens"),
            func.count(LlmUsageEvent.id).label("calls"),
        )
        .where(
            LlmUsageEvent.created_at >= cutoff,
            LlmUsageEvent.conversation_id.isnot(None),
        )
        .group_by(LlmUsageEvent.conversation_id)
    )

    flagged: list[dict] = []
    for row in rows.all():
        conv_id = row[0]
        cost = float(row[1] or 0)
        tool_calls = int(row[2] or 0)
        cache_tokens = float(row[3] or 0)
        input_tokens = float(row[4] or 0)
        calls = int(row[5] or 0)
        denom = cache_tokens + input_tokens
        cache_ratio = (cache_tokens / denom) if denom > 0 else 0.0

        reasons: list[str] = []
        if cost > settings.llm_expensive_conversation_usd:
            reasons.append("high_cost")
        if tool_calls > settings.llm_expensive_tool_calls:
            reasons.append("high_tool_calls")
        if calls > 1 and cache_ratio < settings.llm_low_cache_hit_ratio:
            reasons.append("low_cache_hit_ratio")

        if reasons:
            flagged.append(
                {
                    "conversation_id": conv_id,
                    "cost_usd": round(cost, 6),
                    "tool_calls": tool_calls,
                    "cache_hit_ratio": round(cache_ratio, 4),
                    "calls": calls,
                    "reasons": reasons,
                }
            )

    flagged.sort(key=lambda x: x["cost_usd"], reverse=True)
    return flagged


async def build_daily_founder_llm_report(db: AsyncSession) -> dict:
    """Build yesterday+MTD spend report with anomalies for founder updates."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    yesterday_spend = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd)).where(
            LlmUsageEvent.created_at >= yesterday_start,
            LlmUsageEvent.created_at < today_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    yesterday_spend = float(yesterday_spend or Decimal("0"))

    mtd_spend = await db.scalar(
        select(func.sum(LlmUsageEvent.estimated_cost_usd)).where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
    )
    mtd_spend = float(mtd_spend or Decimal("0"))

    top_models_rows = await db.execute(
        select(
            LlmUsageEvent.model,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("cost"),
        )
        .where(
            LlmUsageEvent.created_at >= month_start,
            LlmUsageEvent.estimated_cost_usd.isnot(None),
        )
        .group_by(LlmUsageEvent.model)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
        .limit(5)
    )
    top_models = [
        {"model": row[0], "cost_usd": round(float(row[1] or 0), 6)}
        for row in top_models_rows.all()
    ]

    top_conv_rows = await db.execute(
        select(
            LlmUsageEvent.conversation_id,
            func.sum(LlmUsageEvent.estimated_cost_usd).label("cost"),
            func.count(LlmUsageEvent.id).label("calls"),
        )
        .where(
            LlmUsageEvent.created_at >= yesterday_start,
            LlmUsageEvent.created_at < today_start,
            LlmUsageEvent.conversation_id.isnot(None),
        )
        .group_by(LlmUsageEvent.conversation_id)
        .order_by(func.sum(LlmUsageEvent.estimated_cost_usd).desc())
        .limit(5)
    )
    top_conversations = [
        {
            "conversation_id": row[0],
            "cost_usd": round(float(row[1] or 0), 6),
            "calls": int(row[2] or 0),
        }
        for row in top_conv_rows.all()
    ]

    cache_tokens = await db.scalar(
        select(func.sum(LlmUsageEvent.cache_hit_tokens)).where(
            LlmUsageEvent.created_at >= yesterday_start,
            LlmUsageEvent.created_at < today_start,
        )
    )
    input_tokens = await db.scalar(
        select(func.sum(LlmUsageEvent.input_tokens)).where(
            LlmUsageEvent.created_at >= yesterday_start,
            LlmUsageEvent.created_at < today_start,
        )
    )
    cache_val = float(cache_tokens or 0)
    input_val = float(input_tokens or 0)
    denom = cache_val + input_val
    cache_hit_ratio = (cache_val / denom) if denom > 0 else 0.0

    daily_budget = float(settings.llm_daily_budget_usd)
    budget_usage_pct = (yesterday_spend / daily_budget * 100) if daily_budget > 0 else 0.0

    expensive_conversations = await detect_expensive_conversations(db, lookback_days=1)

    return {
        "date_utc": yesterday_start.date().isoformat(),
        "yesterday_spend_usd": round(yesterday_spend, 6),
        "month_to_date_spend_usd": round(mtd_spend, 6),
        "top_models_by_cost": top_models,
        "top_expensive_conversations": top_conversations,
        "cache_hit_ratio": round(cache_hit_ratio, 4),
        "budget_usage_percentage": round(budget_usage_pct, 2),
        "expensive_conversation_flags": expensive_conversations,
    }


def format_founder_llm_report(report: dict) -> str:
    """Render compact founder message."""
    models = report.get("top_models_by_cost") or []
    convs = report.get("top_expensive_conversations") or []
    flags = report.get("expensive_conversation_flags") or []

    lines = [
        f"LLM Daily Report ({report.get('date_utc')})",
        f"Yesterday spend: ${report.get('yesterday_spend_usd', 0):.4f}",
        f"MTD spend: ${report.get('month_to_date_spend_usd', 0):.4f}",
        f"Budget usage: {report.get('budget_usage_percentage', 0):.1f}%",
        f"Cache hit ratio: {report.get('cache_hit_ratio', 0):.2%}",
    ]

    if models:
        lines.append("Top models:")
        for m in models[:3]:
            lines.append(f"- {m['model']}: ${m['cost_usd']:.4f}")

    if convs:
        lines.append("Top conversations:")
        for c in convs[:3]:
            cid = (c.get("conversation_id") or "")[-8:]
            lines.append(f"- ...{cid}: ${c['cost_usd']:.4f} ({c.get('calls', 0)} calls)")

    if flags:
        lines.append(f"Flags: {len(flags)} expensive conversation(s)")

    return "\n".join(lines)
