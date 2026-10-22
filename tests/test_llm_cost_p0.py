"""Tests for P0 LLM cost control implementation."""

import asyncio
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage_event import LlmUsageEvent
from app.utils.llm_cost import estimate_cost, get_model_provider
from app.utils.phone_hash import hash_phone
from app.utils.budget_guard import (
    check_global_daily_budget,
    check_global_monthly_budget,
    check_per_user_daily_budget,
    check_all_budgets,
)


class TestLlmCostEstimator:
    """Test cost estimation logic."""

    def test_estimate_cost_gemini_flash(self):
        """Test cost calculation for Gemini 2.5 Flash."""
        # 20,000 input tokens @ $0.15/1M, 200 output tokens @ $0.60/1M
        cost = estimate_cost(
            model="gemini-2.5-flash",
            input_tokens=20000,
            output_tokens=200,
        )
        # 20,000 * 0.15 / 1,000,000 + 200 * 0.60 / 1,000,000
        # = 0.003 + 0.00012 = 0.00312
        assert cost > Decimal("0.003")
        assert cost < Decimal("0.004")

    def test_estimate_cost_with_cache_hit(self):
        """Test cost reduction with cache hit tokens."""
        # Without cache
        cost_no_cache = estimate_cost(
            model="gemini-2.5-flash",
            input_tokens=1000,
            output_tokens=100,
        )
        # With cache (same tokens served from cache, cheaper)
        cost_with_cache = estimate_cost(
            model="gemini-2.5-flash",
            input_tokens=0,
            output_tokens=100,
            cache_hit_tokens=1000,
        )
        # Cached input is 25% of regular input cost (0.037 vs 0.15)
        # Total cost reduced from 0.00021 to ~0.000097 (about 46% of original)
        assert cost_with_cache < cost_no_cache / 2

    def test_estimate_cost_classifier(self):
        """Test cost for cheaper classifier model."""
        cost = estimate_cost(
            model="gemini-2.5-flash-lite",
            input_tokens=200,
            output_tokens=2,
        )
        assert cost > Decimal("0")
        assert cost < Decimal("0.0001")

    def test_estimate_cost_unknown_model(self):
        """Test handling of unknown model — should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown model"):
            estimate_cost(
                model="unknown-model",
                input_tokens=1000,
                output_tokens=100,
            )

    def test_estimate_cost_deepseek(self):
        """Test cost calculation for DeepSeek fallback model."""
        cost = estimate_cost(
            model="deepseek-v4-flash",
            input_tokens=200,
            output_tokens=2,
        )
        assert cost > Decimal("0")

    def test_deepseek_rates_match_the_published_price_list(self):
        """Pin v4-flash rates: $0.14 in / $0.28 out per 1M.

        Verified 2026-08-07 against api-docs.deepseek.com. These had silently
        inherited `deepseek-chat` (v3) rates through a model rename, which
        overstated cost ~2x — and they feed `budget_guard`'s daily cap, so
        drift here quietly mis-enforces spend in whichever direction it is
        wrong. Update this test and the table together, with a source.
        """
        from app.utils.llm_cost import MODEL_PRICING

        rates = MODEL_PRICING["deepseek-v4-flash"]
        assert rates["input_cost_per_1m"] == Decimal("0.14")
        assert rates["output_cost_per_1m"] == Decimal("0.28")
        assert rates["cache_hit_cost_per_1m"] == Decimal("0.0028")

        # 1M in + 1M out should be exactly $0.42.
        assert estimate_cost(
            model="deepseek-v4-flash",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        ) == Decimal("0.42")

    def test_estimate_cost_embedding(self):
        """Test cost for embedding model (very cheap)."""
        cost = estimate_cost(
            model="text-embedding-004",
            input_tokens=100,
            output_tokens=0,
        )
        # text-embedding-004 is $0.00002 per 1M input tokens
        # 100 * 0.00002 / 1,000,000 = 0.000000002
        assert cost > Decimal("0")
        assert cost < Decimal("0.000001")

    def test_get_model_provider(self):
        """Test provider lookup."""
        assert get_model_provider("gemini-2.5-flash") == "google_genai"
        assert get_model_provider("deepseek-v4-flash") == "deepseek"
        assert get_model_provider("unknown") is None


class TestPhoneHash:
    """Test phone hashing for PII protection."""

    def test_hash_phone_stable(self):
        """Test that hashing is stable (same input → same output)."""
        phone = "919876543210"
        hash1 = hash_phone(phone)
        hash2 = hash_phone(phone)
        assert hash1 == hash2

    def test_hash_phone_different(self):
        """Test that different phones produce different hashes."""
        hash1 = hash_phone("919876543210")
        hash2 = hash_phone("919876543211")
        assert hash1 != hash2

    def test_hash_phone_length(self):
        """Test that hashes are reasonable length (16 hex chars = 8 bytes)."""
        hash_val = hash_phone("919876543210")
        assert len(hash_val) == 16
        assert all(c in "0123456789abcdef" for c in hash_val)

    def test_hash_phone_irreversible(self):
        """Test that hashes cannot be reversed."""
        phone = "919876543210"
        hash_val = hash_phone(phone)
        # Cannot reverse a SHA256 hash (preimage resistance)
        assert phone not in hash_val
        assert hash_val != phone


@pytest.mark.asyncio
async def test_llm_usage_event_model_exists(db: AsyncSession):
    """Test that LlmUsageEvent table exists and can be written to."""
    from app.utils.id_gen import generate_id

    event = LlmUsageEvent(
        id=generate_id("llu"),
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        phase="browse",
        conversation_id="test-session",
        phone_hash=hash_phone("919876543210"),
        input_tokens=1000,
        output_tokens=50,
        cache_hit_tokens=0,
        total_tokens=1050,
        estimated_cost_usd=Decimal("0.0015"),
        latency_ms=250,
        success=True,
        error_type=None,
        fallback_used=False,
        tool_calls_count=1,
    )
    db.add(event)
    await db.flush()

    # Verify it was written
    result = await db.scalar(select(LlmUsageEvent).where(LlmUsageEvent.id == event.id))
    assert result is not None
    assert result.provider == "google_genai"
    assert result.model == "gemini-2.5-flash"
    assert result.purpose == "main_agent"


@pytest.mark.asyncio
async def test_llm_usage_event_with_error(db: AsyncSession):
    """Test logging of failed LLM calls."""
    from app.utils.id_gen import generate_id

    event = LlmUsageEvent(
        id=generate_id("llu"),
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        phase="browse",
        conversation_id="test-session",
        phone_hash=hash_phone("919876543210"),
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        estimated_cost_usd=Decimal("0"),
        latency_ms=12000,
        success=False,
        error_type="timeout",
        fallback_used=True,
        tool_calls_count=None,
    )
    db.add(event)
    await db.flush()

    result = await db.scalar(select(LlmUsageEvent).where(LlmUsageEvent.id == event.id))
    assert result is not None
    assert result.success is False
    assert result.error_type == "timeout"
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_tool_loop_cap_config():
    """Test that tool loop cap is configured."""
    from app.core.config import settings

    assert settings.agent_max_tool_rounds == 5


@pytest.mark.asyncio
async def test_budget_guard_config():
    """Test that budget guard config exists."""
    from app.core.config import settings

    assert settings.llm_daily_budget_usd > 0
    assert settings.llm_monthly_budget_usd > 0
    assert settings.llm_per_user_daily_budget_usd > 0


@pytest.mark.asyncio
async def test_global_daily_budget_check(db: AsyncSession):
    """Test global daily budget enforcement."""
    from app.core.config import settings
    from app.utils.id_gen import generate_id

    # Add a large cost event for today
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal(str(settings.llm_daily_budget_usd - 0.1)),  # Just under limit
        latency_ms=100,
        success=True,
    )
    db.add(event)
    await db.commit()

    # Should be allowed
    ok = await check_global_daily_budget(db)
    assert ok is True

    # Add another event that would exceed budget
    event2 = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal("0.5"),  # Pushes over limit
        latency_ms=100,
        success=True,
    )
    db.add(event2)
    await db.commit()

    # Should now be rejected
    ok = await check_global_daily_budget(db)
    assert ok is False


@pytest.mark.asyncio
async def test_global_monthly_budget_check(db: AsyncSession):
    """Test global monthly budget enforcement."""
    from app.core.config import settings
    from app.utils.id_gen import generate_id

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Add a large cost event this month
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=month_start,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal(str(settings.llm_monthly_budget_usd - 10.0)),
        latency_ms=100,
        success=True,
    )
    db.add(event)
    await db.commit()

    # Should be allowed
    ok = await check_global_monthly_budget(db)
    assert ok is True

    # Add another event that exceeds monthly budget
    event2 = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=month_start,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal("20.0"),
        latency_ms=100,
        success=True,
    )
    db.add(event2)
    await db.commit()

    # Should now be rejected
    ok = await check_global_monthly_budget(db)
    assert ok is False


@pytest.mark.asyncio
async def test_per_user_daily_budget_check(db: AsyncSession):
    """Test per-user daily budget enforcement."""
    from app.core.config import settings
    from app.utils.id_gen import generate_id

    phone_hash = hash_phone("919876543210")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Add a user cost event
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        phone_hash=phone_hash,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal(str(settings.llm_per_user_daily_budget_usd - 0.1)),
        latency_ms=100,
        success=True,
    )
    db.add(event)
    await db.commit()

    # Should be allowed
    ok = await check_per_user_daily_budget(db, phone_hash)
    assert ok is True

    # Add another event that exceeds user budget
    event2 = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        phone_hash=phone_hash,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        estimated_cost_usd=Decimal("0.5"),
        latency_ms=100,
        success=True,
    )
    db.add(event2)
    await db.commit()

    # Should now be rejected
    ok = await check_per_user_daily_budget(db, phone_hash)
    assert ok is False


@pytest.mark.asyncio
async def test_check_all_budgets(db: AsyncSession):
    """Test that check_all_budgets correctly checks all constraints."""

    phone_hash = hash_phone("919876543210")

    # All budgets should pass initially
    ok, violation = await check_all_budgets(db, phone_hash)
    assert ok is True
    assert violation is None

    # With disabled budgets (zero values), should always pass
    with patch("app.utils.budget_guard.settings.llm_daily_budget_usd", 0):
        ok, violation = await check_all_budgets(db, phone_hash)
        assert ok is True


@pytest.mark.asyncio
async def test_budget_blocked_event_recorded(db: AsyncSession):
    """Test that a budget-blocked LLM call is recorded with appropriate error_type."""
    from app.utils.id_gen import generate_id

    phone_hash = hash_phone("919876543210")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Simulate a budget-blocked call
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        phone_hash=phone_hash,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        phase="browse",
        conversation_id="test-session",
        input_tokens=None,
        output_tokens=None,
        cache_hit_tokens=None,
        total_tokens=None,
        estimated_cost_usd=Decimal("0"),
        latency_ms=10,
        success=False,
        error_type="budget_limit_per_user_daily",
        fallback_used=True,
        tool_calls_count=None,
    )
    db.add(event)
    await db.commit()

    # Verify it was recorded
    result = await db.scalar(
        select(LlmUsageEvent).where(
            LlmUsageEvent.id == event.id,
            LlmUsageEvent.error_type == "budget_limit_per_user_daily",
        )
    )
    assert result is not None
    assert result.success is False
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_failed_llm_call_recorded(db: AsyncSession):
    """Test that failed LLM calls are recorded with error details."""
    from app.utils.id_gen import generate_id

    phone_hash = hash_phone("919876543210")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Simulate a timeout failure
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        phone_hash=phone_hash,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        phase="browse",
        conversation_id="test-session",
        input_tokens=1000,
        output_tokens=None,
        cache_hit_tokens=0,
        total_tokens=1000,
        estimated_cost_usd=Decimal("0.00015"),
        latency_ms=30000,
        success=False,
        error_type="timeout",
        fallback_used=True,
        tool_calls_count=None,
    )
    db.add(event)
    await db.commit()

    # Verify it was recorded
    result = await db.scalar(
        select(LlmUsageEvent).where(LlmUsageEvent.id == event.id)
    )
    assert result is not None
    assert result.success is False
    assert result.error_type == "timeout"
    assert result.latency_ms == 30000
    assert result.fallback_used is True


@pytest.mark.asyncio
async def test_tool_loop_limit_event_recorded(db: AsyncSession):
    """Test that tool loop limit exceeded event is recorded."""
    from app.utils.id_gen import generate_id

    phone_hash = hash_phone("919876543210")
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Simulate tool loop limit exceeded
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        phone_hash=phone_hash,
        provider="google_genai",
        model="gemini-2.5-flash",
        purpose="main_agent",
        phase="browse",
        conversation_id="test-session",
        input_tokens=50000,
        output_tokens=5000,
        cache_hit_tokens=0,
        total_tokens=55000,
        estimated_cost_usd=Decimal("0.033"),
        latency_ms=15000,
        success=False,
        error_type="tool_loop_limit_exceeded",
        fallback_used=True,
        tool_calls_count=6,  # Exceeded limit of 5
    )
    db.add(event)
    await db.commit()

    # Verify it was recorded
    result = await db.scalar(
        select(LlmUsageEvent).where(LlmUsageEvent.id == event.id)
    )
    assert result is not None
    assert result.success is False
    assert result.error_type == "tool_loop_limit_exceeded"
    assert result.tool_calls_count == 6


@pytest.mark.asyncio
async def test_classifier_usage_event_recorded(db: AsyncSession):
    """Test that classifier usage event is recorded."""
    from app.utils.id_gen import generate_id

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Simulate classifier logging
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        provider="google_genai",
        model="gemini-2.5-flash-lite",
        purpose="classifier",
        phase=None,
        conversation_id=None,
        phone_hash=None,  # Classifier doesn't track per-user
        input_tokens=133,
        output_tokens=2,
        cache_hit_tokens=None,
        total_tokens=135,
        estimated_cost_usd=estimate_cost(
            model="gemini-2.5-flash-lite",
            input_tokens=133,
            output_tokens=2,
        ),
        latency_ms=95,
        success=True,
        error_type=None,
        fallback_used=False,
        tool_calls_count=None,
    )
    db.add(event)
    await db.commit()

    # Verify it was recorded
    result = await db.scalar(
        select(LlmUsageEvent).where(
            LlmUsageEvent.id == event.id,
            LlmUsageEvent.purpose == "classifier",
        )
    )
    assert result is not None
    assert result.model == "gemini-2.5-flash-lite"
    assert result.purpose == "classifier"
    assert result.success is True


@pytest.mark.asyncio
async def test_embedding_usage_event_recorded(db: AsyncSession):
    """Test that embedding usage event is recorded."""
    from app.utils.id_gen import generate_id

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Simulate embedding logging
    event = LlmUsageEvent(
        id=generate_id("llu"),
        created_at=today_start,
        provider="google_genai",
        model="text-embedding-004",
        purpose="embedding",
        phase=None,
        conversation_id=None,
        phone_hash=None,  # Embedding doesn't track per-user
        input_tokens=100,
        output_tokens=0,
        cache_hit_tokens=None,
        total_tokens=100,
        estimated_cost_usd=estimate_cost(
            model="text-embedding-004",
            input_tokens=100,
            output_tokens=0,
        ),
        latency_ms=45,
        success=True,
        error_type=None,
        fallback_used=False,
        tool_calls_count=None,
    )
    db.add(event)
    await db.commit()

    # Verify it was recorded
    result = await db.scalar(
        select(LlmUsageEvent).where(
            LlmUsageEvent.id == event.id,
            LlmUsageEvent.purpose == "embedding",
        )
    )
    assert result is not None
    assert result.model == "text-embedding-004"
    assert result.purpose == "embedding"
    assert result.success is True


@pytest.mark.asyncio
async def test_intent_router_never_raises_when_the_api_fails(db: AsyncSession):
    """Fail-open contract: a DeepSeek outage must not break a turn.

    The router runs on the WhatsApp reply path. If it raised, every message
    would fail while Gemini was perfectly healthy.
    """
    from app.bot import intent_router

    async def _boom(*_a, **_kw):
        raise RuntimeError("deepseek exploded")

    with patch.object(intent_router, "_call_model", _boom):
        result = await intent_router.route(db, "919876543210", "test message")

    assert result.is_degraded
    assert result.intent == "other"
    assert result.likely_tools == ()




class TestLlmUsageEventFields:
    """Test that LlmUsageEvent has all required fields."""

    def test_llm_usage_event_schema(self):
        """Verify all required fields exist."""
        required_fields = [
            "id",
            "created_at",
            "provider",
            "model",
            "purpose",
            "input_tokens",
            "output_tokens",
            "cache_hit_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "latency_ms",
            "success",
            "error_type",
            "fallback_used",
            "tool_calls_count",
            "phone_hash",
            "conversation_id",
            "phase",
            "metadata_json",
        ]

        for field in required_fields:
            assert hasattr(LlmUsageEvent, field), f"LlmUsageEvent missing field: {field}"


# --- Intent router spend must be visible to the budget guard ----------------


@pytest.mark.asyncio
async def test_intent_router_writes_a_usage_row(db: AsyncSession, monkeypatch):
    """Routing spend MUST reach llm_usage_events.

    budget_guard enforces the daily/monthly caps by SUMMING that table. The
    router runs on nearly every turn with a far larger prompt than the old
    classifier, so a missing row is spend the guard cannot see — it would
    silently erode the very budget it exists to enforce.
    """
    from contextlib import asynccontextmanager

    import app.db.session as db_session
    from app.bot import intent_router

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(db_session, "async_session", _session)

    # Real usage payload shape, captured from the live DeepSeek API.
    intent_router._log_usage(
        {"prompt_tokens": 1394, "completion_tokens": 46}, 1832.0, success=True,
    )
    await asyncio.sleep(0.2)  # fire-and-forget task

    rows = (
        await db.execute(
            select(LlmUsageEvent).where(LlmUsageEvent.purpose == "intent_router")
        )
    ).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    # Provider-REPORTED tokens, not a hardcoded estimate.
    assert row.input_tokens == 1394
    assert row.output_tokens == 46
    assert row.estimated_cost_usd is not None
    assert row.estimated_cost_usd > 0
    assert row.latency_ms == 1832


@pytest.mark.asyncio
async def test_usage_logging_is_skipped_when_no_tokens_reported(
    db: AsyncSession, monkeypatch,
):
    """No tokens reported → nothing spent → no row (and no zero-cost noise)."""
    from contextlib import asynccontextmanager

    import app.db.session as db_session
    from app.bot import intent_router

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(db_session, "async_session", _session)

    intent_router._log_usage({}, 100.0, success=False)
    await asyncio.sleep(0.2)

    rows = (
        await db.execute(
            select(LlmUsageEvent).where(LlmUsageEvent.purpose == "intent_router")
        )
    ).scalars().all()
    assert rows == []
