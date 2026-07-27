"""Rate limiter — prevents abuse on webhook endpoints.

Per-phone rate limiting to protect against:
- Gemini API cost abuse (someone spamming messages)
- Webhook endpoint abuse (fake requests if signature not enforced)

When ``REDIS_URL`` is configured, ``allow()`` uses a Redis fixed-window
counter shared by all workers — required for WEB_WORKERS > 1, otherwise
each worker grants the full quota independently. Without Redis (or on a
Redis error), it falls back to the original in-memory token bucket, which
is correct only for single-process deployments.
"""

import logging
import time
from collections import defaultdict

from redis.exceptions import RedisError

from app.core.config import settings
from app.utils.redis_client import get_redis

logger = logging.getLogger("wishkr.rate_limit")


class RateLimiter:
    """Rate limiter keyed by identifier (phone number, IP, etc)."""

    def __init__(self, max_requests: int, window_seconds: int, name: str = ""):
        self._max_requests = max_requests
        self._window = window_seconds
        # Namespaces the Redis keys so two limiters with the same key
        # (e.g. one IP hitting two endpoints) don't share a counter.
        self._name = name or f"rl{id(self)}"
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def allow(self, key: str) -> bool:
        """Distributed check when Redis is available, in-memory otherwise."""
        r = get_redis()
        if r is None:
            return self.is_allowed(key)
        # Fixed window: INCR + EXPIRE-on-first-hit. Slightly coarser than the
        # sliding in-memory window (a burst straddling the boundary can see up
        # to 2x quota) — acceptable for abuse protection, and atomic enough
        # without scripting.
        redis_key = f"wk:rl:{self._name}:{key}"
        try:
            count = await r.incr(redis_key)
            if count == 1:
                await r.expire(redis_key, self._window)
            return count <= self._max_requests
        except (RedisError, OSError) as e:
            logger.warning("Rate limiter Redis error — in-memory fallback: %s", e)
            return self.is_allowed(key)

    def is_allowed(self, key: str) -> bool:
        """In-memory check (single-process only)."""
        now = time.monotonic()
        timestamps = self._requests[key]

        # Prune expired timestamps
        self._requests[key] = [t for t in timestamps if now - t < self._window]

        if len(self._requests[key]) >= self._max_requests:
            return False

        self._requests[key].append(now)
        return True

    def cleanup(self) -> None:
        """Remove stale entries to prevent memory growth.

        After ``_window`` seconds with no new request, the entry contributes
        nothing to rate-limiting decisions (any next ``is_allowed`` would
        prune all timestamps before checking). Evict at exactly ``_window``
        instead of the previous 2× safety margin — under festival-spike
        load with thousands of unique phones / IPs, the 2× headroom doubles
        memory for no protective benefit.
        """
        now = time.monotonic()
        stale_keys = [
            k for k, timestamps in self._requests.items()
            if not timestamps or (now - timestamps[-1]) > self._window
        ]
        for k in stale_keys:
            del self._requests[k]


# Per-phone: max 20 messages per 60 seconds (generous for real users, blocks abuse)
message_rate_limiter = RateLimiter(
    max_requests=settings.rate_limit_messages_per_minute,
    window_seconds=60,
    name="messages",
)

# Per-IP on webhook endpoint: max 100 requests per 60 seconds
webhook_rate_limiter = RateLimiter(
    max_requests=settings.rate_limit_webhooks_per_minute,
    window_seconds=60,
    name="webhooks",
)
