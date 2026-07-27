from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.llm_usage_event import LlmUsageEvent
from app.utils.id_gen import generate_id
from app.utils.llm_founder_report import (
    build_daily_founder_llm_report,
    detect_expensive_conversations,
)


def _event(
    *,
    conv: str,
    model: str = "gemini-2.5-flash",
    cost: str = "0.10",
    calls: int = 1,
    tools: int = 1,
    cache: int = 0,
    inp: int = 100,
    created_at: datetime,
):
    return LlmUsageEvent(
        id=generate_id("llu"),
        created_at=created_at,
        provider="google_genai",
        model=model,
        purpose="main_agent",
        phase="browse",
        conversation_id=conv,
        phone_hash="abc123",
        input_tokens=inp,
        output_tokens=20,
        cache_hit_tokens=cache,
        total_tokens=inp + 20,
        estimated_cost_usd=Decimal(cost),
        latency_ms=120,
        success=True,
        error_type=None,
        fallback_used=False,
        tool_calls_count=tools,
    )


@pytest.mark.asyncio
async def test_detect_expensive_conversations_flags_cost_tools_cache(db):
    now = datetime.now(timezone.utc)
    db.add(_event(conv="c1", cost="1.10", tools=2, cache=0, inp=100, created_at=now - timedelta(hours=2)))
    db.add(_event(conv="c2", cost="0.05", tools=20, cache=0, inp=200, created_at=now - timedelta(hours=2)))
    db.add(_event(conv="c3", cost="0.03", tools=1, cache=1, inp=1000, created_at=now - timedelta(hours=2)))
    db.add(_event(conv="c3", cost="0.02", tools=1, cache=0, inp=900, created_at=now - timedelta(hours=2)))
    await db.commit()

    flags = await detect_expensive_conversations(db, lookback_days=1)
    by_conv = {f["conversation_id"]: set(f["reasons"]) for f in flags}

    assert "high_cost" in by_conv["c1"]
    assert "high_tool_calls" in by_conv["c2"]
    assert "low_cache_hit_ratio" in by_conv["c3"]


@pytest.mark.asyncio
async def test_build_daily_founder_report_contains_required_fields(db):
    now = datetime.now(timezone.utc)
    yesterday = now.replace(hour=1, minute=0, second=0, microsecond=0) - timedelta(days=1)
    db.add(_event(conv="c10", model="gemini-2.5-flash", cost="0.30", tools=2, cache=50, inp=200, created_at=yesterday))
    db.add(_event(conv="c11", model="gemini-2.5-flash-lite", cost="0.10", tools=1, cache=10, inp=100, created_at=yesterday))
    await db.commit()

    report = await build_daily_founder_llm_report(db)

    assert "yesterday_spend_usd" in report
    assert "month_to_date_spend_usd" in report
    assert "top_models_by_cost" in report
    assert "top_expensive_conversations" in report
    assert "cache_hit_ratio" in report
    assert "budget_usage_percentage" in report


def test_p3_threshold_settings_present():
    assert settings.llm_expensive_conversation_usd > 0
    assert settings.llm_expensive_tool_calls > 0
    assert 0 <= settings.llm_low_cache_hit_ratio <= 1
