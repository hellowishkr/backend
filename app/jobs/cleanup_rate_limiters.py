"""Periodic sweep of in-memory rate limiter dicts.

Each `RateLimiter.is_allowed()` only prunes timestamps for the key being
checked — keys that are hit once and never again stay forever. Over months
this grows proportional to unique IPs/phones seen. This job evicts stale
keys on a slow interval; correctness does not depend on it, only memory.

Also prunes the per-phone `_LAST_ACK` map maintained by the WhatsApp
webhook for the same reason: stale-but-never-pruned otherwise.
"""

import logging

from app.api.vendor_leads import _partner_rate_limiter
from app.utils.rate_limit import message_rate_limiter, webhook_rate_limiter
from app.webhooks.whatsapp import _burst_rate_limiter, cleanup_last_ack

logger = logging.getLogger("wishkr.cleanup")


async def cleanup_rate_limiters() -> None:
    for name, limiter in (
        ("messages", message_rate_limiter),
        ("webhooks", webhook_rate_limiter),
        ("partners", _partner_rate_limiter),
        ("burst", _burst_rate_limiter),
    ):
        before = len(limiter._requests)
        limiter.cleanup()
        after = len(limiter._requests)
        if before != after:
            logger.info("rate_limiter_cleanup name=%s evicted=%d remaining=%d", name, before - after, after)

    evicted_ack = cleanup_last_ack()
    if evicted_ack:
        logger.info("last_ack_cleanup evicted=%d", evicted_ack)
