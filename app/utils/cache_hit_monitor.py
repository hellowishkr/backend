"""Cache-hit monitoring for LLM implicit caching efficiency."""

import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_usage_event import LlmUsageEvent

logger = logging.getLogger("wishkr.cache_monitor")


async def check_cache_hit_efficiency(db: AsyncSession, threshold_hours: int = 24) -> dict:
    """Check cache-hit efficiency for returning sessions.

    Returns warnings if sessions are making repeated calls without cache hits,
    which indicates either:
    1. New context on each call (expected for first calls)
    2. Cache expiration / token overflow (needs monitoring)
    3. Static prefix cache not warming up (needs investigation)

    Args:
        db: AsyncSession
        threshold_hours: Look back this many hours for "returning" sessions

    Returns:
        dict with cache efficiency metrics and warnings
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)

    # Find sessions with multiple calls
    session_calls = await db.execute(
        select(
            LlmUsageEvent.conversation_id,
            func.count(LlmUsageEvent.id).label("call_count"),
            func.sum(LlmUsageEvent.cache_hit_tokens).label("total_cache_hits"),
            func.sum(
                func.coalesce(LlmUsageEvent.cache_hit_tokens, 0)
            ).label("cache_sum"),
        )
        .where(
            LlmUsageEvent.created_at >= cutoff,
            LlmUsageEvent.conversation_id.isnot(None),
            LlmUsageEvent.purpose == "main_agent",
        )
        .group_by(LlmUsageEvent.conversation_id)
        .having(func.count(LlmUsageEvent.id) > 1)
    )

    sessions_by_cache = {"with_caching": 0, "without_caching": 0, "partial_caching": 0}
    zero_cache_sessions = []

    for row in session_calls.all():
        session_id = row[0]
        call_count = row[1]
        total_cache_hits = float(row[2] or 0)

        if total_cache_hits == 0:
            sessions_by_cache["without_caching"] += 1
            zero_cache_sessions.append({"session_id": session_id, "calls": call_count})
        elif total_cache_hits > 0 and call_count > 1:
            sessions_by_cache["with_caching"] += 1
        else:
            sessions_by_cache["partial_caching"] += 1

    # Log warnings for sessions without caching
    warnings = []
    if len(zero_cache_sessions) > 5:
        warning = (
            f"Found {len(zero_cache_sessions)} returning sessions making "
            f"{sum(s['calls'] for s in zero_cache_sessions)} calls with ZERO cache hits. "
            f"This may indicate cache expiration, token overflow, or context reset issues."
        )
        warnings.append(warning)
        logger.warning(warning)

    return {
        "threshold_hours": threshold_hours,
        "sessions_analyzed": sum(sessions_by_cache.values()),
        "sessions_by_cache": sessions_by_cache,
        "zero_cache_sessions_count": len(zero_cache_sessions),
        "zero_cache_sessions_sample": zero_cache_sessions[:5],  # First 5 for debugging
        "warnings": warnings,
    }


async def get_cache_hit_ratio(db: AsyncSession, hours: int = 24) -> float:
    """Calculate cache hit ratio (cached tokens / (cached + regular input tokens)).

    A high ratio (>0.3) indicates good cache reuse.
    A low ratio (<0.1) indicates most calls are cache misses (cold starts).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    total_cache = await db.scalar(
        select(func.sum(LlmUsageEvent.cache_hit_tokens)).where(
            LlmUsageEvent.created_at >= cutoff,
            LlmUsageEvent.cache_hit_tokens.isnot(None),
        )
    )
    total_input = await db.scalar(
        select(func.sum(LlmUsageEvent.input_tokens)).where(
            LlmUsageEvent.created_at >= cutoff,
            LlmUsageEvent.input_tokens.isnot(None),
        )
    )

    total_cache = float(total_cache or 0)
    total_input = float(total_input or 0)

    if (total_cache + total_input) == 0:
        return 0.0

    return total_cache / (total_cache + total_input)
