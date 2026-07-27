"""Lazy shared Redis client for cross-worker state.

Used by the rate limiters and the outbound reply dedup/ACK guards so that
multiple uvicorn workers (WEB_WORKERS > 1) agree on per-phone/per-IP state.

Design rules:

* ``settings.redis_url`` empty → ``get_redis()`` returns None and every
  caller falls back to its original in-memory behaviour (local dev, tests,
  single-worker deployments).
* Redis errors at call time must never take down a webhook turn — callers
  catch ``RedisError``/``OSError`` and fall back in-memory for that call.
  A degraded Redis means slightly weaker dedup, not an outage.
"""

import logging

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger("wishkr.redis")

_client: Redis | None = None
_client_failed_logged = False


def get_redis() -> Redis | None:
    """Return the shared async Redis client, or None when not configured.

    The client is created lazily and never pinged here — redis-py connects
    on first command, and callers already handle per-call failures.
    """
    global _client, _client_failed_logged
    if not settings.redis_url:
        return None
    if _client is None:
        try:
            _client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception:
            if not _client_failed_logged:
                _client_failed_logged = True
                logger.exception("Redis client init failed — falling back to in-memory state")
            return None
    return _client


async def close_redis() -> None:
    """Close the shared client (app shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.warning("Redis close failed", exc_info=True)
        _client = None
