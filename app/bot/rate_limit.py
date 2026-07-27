"""In-process per-phone rate limiter.

Why in-process (not Redis): single-instance deployment. When you scale to 2+
replicas, swap `_buckets` for a Redis INCR + EXPIRE. The public API
(check_and_bump) stays identical.

Design: sliding window via deque of timestamps. O(n) in the window size (~20
for our limits), memory-bounded by pruning old entries on every call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger("wishkr.ratelimit")


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: float  # 0 when allowed


# phone -> deque[timestamp_seconds]
_buckets: dict[str, deque[float]] = {}
# phone -> lock. Per-phone locking so one slow user can't block the rest.
_phone_locks: dict[str, asyncio.Lock] = {}
# Protects _phone_locks and _buckets structure mutations (eviction, new entries).
# Held for microseconds — not a concurrency bottleneck.
_registry_lock = asyncio.Lock()

# Keep a cap on distinct phones in memory to prevent unbounded growth from abuse.
# At 20 msg/min limit, 10k phones = ~200k entries max, ~16 MB worst case.
_MAX_PHONES = 10_000


async def _get_phone_lock(phone: str) -> asyncio.Lock:
    """Return the per-phone lock, creating it under the registry guard."""
    lock = _phone_locks.get(phone)
    if lock is not None:
        return lock
    async with _registry_lock:
        # Double-check after acquiring
        lock = _phone_locks.get(phone)
        if lock is None:
            # Enforce phone cap — evict the oldest bucket if we're at capacity.
            if len(_phone_locks) >= _MAX_PHONES:
                oldest = min(
                    _buckets,
                    key=lambda p: _buckets[p][0] if _buckets[p] else 0.0,
                    default=None,
                )
                if oldest is not None:
                    _buckets.pop(oldest, None)
                    _phone_locks.pop(oldest, None)
            lock = asyncio.Lock()
            _phone_locks[phone] = lock
        return lock


async def check_and_bump(phone: str) -> RateLimitResult:
    """Record a request for `phone`; return whether it's within the limit.

    Uses `settings.rate_limit_messages_per_minute` as the per-phone cap.
    Locking is per-phone so a slow check on user A does not block user B.
    """
    limit = max(1, int(settings.rate_limit_messages_per_minute))
    window_seconds = 60.0
    now = time.monotonic()

    lock = await _get_phone_lock(phone)
    async with lock:
        bucket = _buckets.setdefault(phone, deque())

        # Drop timestamps outside the window
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(0.0, window_seconds - (now - bucket[0]))
            return RateLimitResult(allowed=False, retry_after_seconds=retry_after)

        bucket.append(now)
        return RateLimitResult(allowed=True, retry_after_seconds=0.0)
