"""Meta WhatsApp Cloud API webhook — receive messages and route to AI agent.

Two endpoints:
  GET  /webhooks/whatsapp  — Meta verification challenge
  POST /webhooks/whatsapp  — incoming messages + status updates
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Query, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.session import async_session
from app.bot.agent import handle_message
from app.bot.action_router import try_handle_product_urls_before_classifier
from app.bot.canned_responses import WISHLIST_ADDED_RESPONSE
from app.bot.intent_classifier import is_rule_based_affirmative
from sqlalchemy import update
from app.models import FailedOutboundMessage, ProcessedMessage, User
from app.services.whatsapp import whatsapp_service
from app.utils.db_retry import is_transient_db_error, retry_db_operation
from app.utils.debounce import message_debouncer
from app.utils.id_gen import generate_id
from app.utils.link_utils import extract_urls, is_product_link
from app.utils.observability import latency_bucket, mask_phone, metrics
from app.utils.rate_limit import RateLimiter, message_rate_limiter, webhook_rate_limiter
from app.utils.redis_client import get_redis

from redis.exceptions import RedisError

# --- Hardening knobs ---------------------------------------------------------
# Hard ceiling on the ACK round-trip. If WhatsApp's API is slow we'd rather
# drop the ACK than delay the heavy work that follows. The actual ACK call
# normally completes in 100–250 ms.
_ACK_SEND_TIMEOUT_SECONDS = 0.5

# Background-processing ceiling. Sized to comfortably cover the worst legit
# case: debounce wait (3 s) + Gemini turn (AGENT_TURN_TIMEOUT_SECONDS = 6 s)
# + small buffer. Anything longer is stuck and should be cancelled so the
# event loop doesn't accumulate zombie coroutines under load.
_BACKGROUND_TIMEOUT_SECONDS = 25.0

# Anti-spam burst guard, *layered on top of* the existing 20 msg / 60 s limit.
# Catches loops and double-tap floods that the broader limiter would still let
# through inside a 3 s window.
_burst_rate_limiter = RateLimiter(max_requests=5, window_seconds=5, name="burst")

# Hard ceiling on concurrent background tasks. Sheds load before we run out
# of Gemini quota / DB connections / file descriptors. Sized for ~1k active
# users with sub-second turn times; tune up if you scale vertically.
MAX_ACTIVE_TASKS = 500
_BACKPRESSURE_TEXT = (
    "Too many requests right now — try again in a few seconds 🙏"
)

# Track in-flight background tasks so they aren't garbage-collected mid-run
# and so we can observe load (`len(_active_tasks)`).
_active_tasks: set[asyncio.Task] = set()


def _spawn_tracked(coro) -> asyncio.Task:
    """Create a background task and keep a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


def _register_task(task: asyncio.Task) -> asyncio.Task:
    """Register an *existing* task into the active-task set.

    Used for tasks created elsewhere (e.g. the debouncer's internal timer
    task) so `drain_active_tasks` can cancel them on shutdown and so the
    backpressure cap reflects real load.
    """
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


async def drain_active_tasks(timeout: float = 5.0) -> None:
    """Cancel + await all in-flight background tasks. Called from app shutdown.

    Best-effort: gives each task a chance to finish gracefully within `timeout`,
    then cancels anything still running so the process can exit.
    """
    if not _active_tasks:
        return
    pending = list(_active_tasks)
    logger.info("Draining %d active background tasks", len(pending))
    for t in pending:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Drain timed out; %d tasks may be orphaned", len(_active_tasks))


router = APIRouter()
logger = logging.getLogger("wishkr.webhooks.whatsapp")

# WhatsApp webhook idempotency — Meta retries if it doesn't get a 200 fast.
# Track recently-seen `wamid`s so retries become no-ops.
# Single-instance: in-process LRU is enough (~200 KB at cap, covers Meta's retry
# window). Multi-instance: swap for Redis SETEX with a 10-min TTL.
_SEEN_MESSAGE_IDS: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 10_000
_PROCESSING_STALE_SECONDS = 60


def _remember_processed_wamid(wamid: str) -> None:
    """Best-effort hot cache on top of durable DB-backed idempotency."""
    if not wamid:
        return
    _SEEN_MESSAGE_IDS[wamid] = None
    if len(_SEEN_MESSAGE_IDS) > _SEEN_MAX:
        _SEEN_MESSAGE_IDS.popitem(last=False)


async def _claim_processed_message(wamid: str) -> bool:
    """Atomically claim a WhatsApp message id across workers.

    Hardened for transient DB disconnects:
    - Retries once on transient DB errors
    - IntegrityError (race condition) is NOT retried — returns False as expected
    - Preserves idempotency guarantee even under DB blips
    """
    if not wamid:
        return True
    if wamid in _SEEN_MESSAGE_IDS:
        return False

    async def _do_claim() -> bool:
        async with async_session() as db:
            row = await db.scalar(
                select(ProcessedMessage)
                .where(ProcessedMessage.wamid == wamid)
            )

            if row is None:
                db.add(ProcessedMessage(wamid=wamid, status="processing"))
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    return False
            else:
                stale_before = time.time() - _PROCESSING_STALE_SECONDS
                updated_ts = row.updated_at.timestamp() if row.updated_at else 0.0
                if row.status == "done":
                    await db.rollback()
                    return False
                if row.status == "processing" and updated_ts >= stale_before:
                    await db.rollback()
                    return False
                row.status = "processing"
                row.updated_at = datetime.now(timezone.utc)
                await db.commit()

        return True

    try:
        return await retry_db_operation(_do_claim, label="claim_processed_message")
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                "claim_processed_message failed (transient) wamid=%s: %s",
                wamid[:32],
                e,
            )
            return False
        raise


async def _mark_processed_done(wamid: str) -> None:
    """Mark a claimed WhatsApp message as fully handled.

    Hardened for transient DB disconnects:
    - Retries once on transient DB errors
    - Failure is logged but not raised (best-effort)
    - Hot cache is updated even if DB update fails
    """
    if not wamid:
        return

    async def _do_mark() -> None:
        async with async_session() as db:
            row = await db.get(ProcessedMessage, wamid)
            if row is None:
                return
            row.status = "done"
            await db.commit()

    try:
        await retry_db_operation(_do_mark, label="mark_processed_done")
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                "mark_processed_done failed (transient) wamid=%s: %s",
                wamid[:32],
                e,
            )
        else:
            logger.exception("mark_processed_done failed wamid=%s", wamid[:32])

    _remember_processed_wamid(wamid)


async def _mark_processed_failed(wamid: str, phase: str, error_type: str) -> None:
    """Mark a claimed WhatsApp message as failed after processing aborts."""
    if not wamid:
        return

    async def _do_mark() -> None:
        async with async_session() as db:
            row = await db.get(ProcessedMessage, wamid)
            if row is None:
                return
            row.status = "failed"
            await db.commit()

    try:
        await retry_db_operation(_do_mark, label="mark_processed_failed")
    except Exception as e:
        logger.exception(
            "mark_processed_failed failed wamid=%s phase=%s error_type=%s",
            wamid[:32], phase, error_type,
        )
        if is_transient_db_error(e):
            return


def _has_valid_meta_signature(raw_body: bytes, signature: str) -> bool:
    """Return True only when Meta's HMAC signature matches the payload.

    Fail-closed in production: a missing secret indicates a misconfigured
    deploy (env var dropped, KMS unavailable, etc.) and we'd rather drop
    inbound messages than silently accept unverified ones. Dev only stays
    fail-open so local testing without secrets keeps working.
    """
    if not settings.whatsapp_webhook_secret:
        if (settings.app_env or "development").lower() == "production":
            logger.error("WHATSAPP_WEBHOOK_SECRET missing in production — rejecting webhook")
            return False
        logger.warning("WHATSAPP_WEBHOOK_SECRET not set — skipping signature check (dev only)")
        return True

    expected = "sha256=" + hmac.new(
        settings.whatsapp_webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _record_failed_outbound(
    phone: str,
    message_body: str,
    send_kind: str,
    error_message: str,
    template_name: str | None = None,
    template_params: dict | None = None,
) -> None:
    """Persist a failed outbound send for manual replay or background recovery.

    When the original send used an approved utility template, pass
    ``template_name`` + ``template_params`` (a dict like
    ``{"header_params": [...], "body_params": [...]}``). The retry job
    will replay as a template — replaying voucher confirmations as
    free-text outside the 24h CS window would be rejected by Meta and
    burn the retry budget without delivering.
    """
    template_params_json: str | None = None
    if template_name is not None:
        try:
            template_params_json = json.dumps(template_params or {})
        except (TypeError, ValueError):
            # Non-serialisable params → fall back to free-text replay so
            # we don't drop the row entirely.
            logger.exception("template_params not JSON-serialisable for %s", mask_phone(phone))
            template_name = None
    async with async_session() as db:
        db.add(FailedOutboundMessage(
            id=generate_id("out"),
            phone=phone,
            message_body=message_body,
            send_kind=send_kind,
            error_message=error_message[:1000],
            retry_count=2,
            template_name=template_name,
            template_params_json=template_params_json,
        ))
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to persist outbound send failure for %s", mask_phone(phone))


async def record_failed_outbound_message(
    phone: str,
    message_body: str,
    send_kind: str,
    error_message: str,
    template_name: str | None = None,
    template_params: dict | None = None,
) -> None:
    """Public wrapper for jobs/services that need outbound retry capture."""
    await _record_failed_outbound(
        phone,
        message_body,
        send_kind,
        error_message,
        template_name=template_name,
        template_params=template_params,
    )


@router.get("/whatsapp")
async def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification — called once when you register the webhook URL.
    Returns the challenge string if the verify token matches.
    """
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("Webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("Webhook verification failed: mode=%s", mode)
    raise HTTPException(status_code=403, detail="Verification failed")


async def _stamp_last_inbound(phone: str) -> None:
    """Mark the user's most recent inbound time in its own session.

    Kept independent of the message-handling transaction so a handler-side
    rollback can't erase the CS-window evidence — from Meta's perspective
    the user *did* message us, regardless of whether our processing then
    blew up. UPDATE-only: if the phone is brand new and not yet persisted,
    the row will be inserted by the slow-path handler shortly after; the
    next inbound will stamp it.
    """
    try:
        async with async_session() as db:
            await db.execute(
                update(User)
                .where(User.phone == phone)
                .values(last_inbound_at=datetime.now(timezone.utc))
            )
            await db.commit()
    except Exception:
        # Stamping is best-effort — failures here must not block message
        # processing or the webhook ACK.
        logger.exception("last_inbound_at stamp failed for %s", mask_phone(phone))


async def _process_message(phone: str, message_text: str) -> str:
    """Process a message with a fresh DB session (called by debouncer)."""
    async with async_session() as db:
        try:
            response = await handle_message(db, phone, message_text)
            await db.commit()
            return response
        except Exception as e:
            logger.error(
                "Error in message handler for %s: %s",
                mask_phone(phone), e, exc_info=True,
            )
            await db.rollback()
            return "Sorry, something went wrong on my end. Please try again or type *menu* for options."


# Per-phone "last ACK sent" tracker — used for two things:
#   1. Cooldown: don't spam ACKs when a user fires 3 messages in a row.
#   2. Duplicate suppression: if the final reply equals the ACK we already
#      sent (rare, but canned replies can collide), skip the second send.
# Shared via Redis when REDIS_URL is configured (required for WEB_WORKERS>1);
# the dict is the single-process fallback.
_LAST_ACK: dict[str, tuple[float, str]] = {}
# Short cooldown — long enough to dedupe a single multi-message burst, short
# enough that a follow-up question 4 s later still gets its own ACK.
_ACK_COOLDOWN_SECONDS = 3.0
# Redis retention mirrors cleanup_last_ack's 10x-cooldown safety margin.
_ACK_TTL_SECONDS = int(_ACK_COOLDOWN_SECONDS * 10)


def cleanup_last_ack() -> int:
    """Drop `_LAST_ACK` entries whose cooldown is long expired.

    Keeps the dict bounded by *active* users instead of *ever-seen* users.
    The retention factor (10x cooldown) is a safety margin so a slightly
    delayed second message from the same user can still benefit from
    duplicate-suppression in `_send_reply`. Redis entries expire by TTL;
    this only sweeps the in-memory fallback dict.
    """
    cutoff = time.time() - (_ACK_COOLDOWN_SECONDS * 10)
    stale = [k for k, (ts, _) in _LAST_ACK.items() if ts < cutoff]
    for k in stale:
        _LAST_ACK.pop(k, None)
    return len(stale)


async def _ack_get(phone: str) -> tuple[float, str] | None:
    """Last (ts, ack_text) for this phone — Redis first, dict fallback."""
    r = get_redis()
    if r is not None:
        try:
            val = await r.get(f"wk:ack:{phone}")
            if not val:
                return None
            ts_s, _, text = val.partition("|")
            return (float(ts_s), text)
        except (RedisError, OSError, ValueError) as e:
            logger.warning("ack_get Redis error — dict fallback: %s", e)
    return _LAST_ACK.get(phone)


async def _ack_set(phone: str, ts: float, text: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            await r.set(f"wk:ack:{phone}", f"{ts}|{text}", ex=_ACK_TTL_SECONDS)
            return
        except (RedisError, OSError) as e:
            logger.warning("ack_set Redis error — dict fallback: %s", e)
    _LAST_ACK[phone] = (ts, text)


async def _ack_rollback(phone: str, ts: float) -> None:
    """Remove the ACK slot we reserved iff it is still ours (same ts)."""
    r = get_redis()
    if r is not None:
        try:
            val = await r.get(f"wk:ack:{phone}")
            if val and val.partition("|")[0] == str(ts):
                await r.delete(f"wk:ack:{phone}")
            return
        except (RedisError, OSError) as e:
            logger.warning("ack_rollback Redis error — dict fallback: %s", e)
    current = _LAST_ACK.get(phone)
    if current is not None and current[0] == ts:
        _LAST_ACK.pop(phone, None)


# INFRA #16 (done): ``_RECENT_REPLIES`` and ``_LAST_ACK`` live in Redis when
# REDIS_URL is configured, shared across all uvicorn workers — the
# prerequisite for WEB_WORKERS > 1. The dicts remain the single-process
# fallback (local dev, tests, Redis outage). Note the debouncer is still
# process-local: a same-phone burst split across workers makes two agent
# calls instead of one combined call — the per-phone Postgres advisory lock
# serialises them and the Redis reply dedup suppresses identical sends, so
# this degrades cost/UX slightly, not correctness.
_RECENT_REPLIES: dict[tuple[str, str], float] = {}
_REPLY_DEDUP_SECONDS = 60.0


def _reply_key(phone: str, response: str) -> tuple[str, str]:
    return (phone, hashlib.sha256(response.strip().encode()).hexdigest()[:16])


def cleanup_recent_replies() -> int:
    """Drop expired entries from the in-memory outbound-reply dedup cache.

    Redis entries expire by TTL; this only sweeps the fallback dict.
    """
    cutoff = time.time() - _REPLY_DEDUP_SECONDS
    stale = [k for k, ts in _RECENT_REPLIES.items() if ts < cutoff]
    for k in stale:
        _RECENT_REPLIES.pop(k, None)
    return len(stale)


async def _reply_recently_sent(phone: str, body_hash: str) -> bool:
    """True if this exact (phone, body) went out within the dedup window."""
    r = get_redis()
    if r is not None:
        try:
            return bool(await r.exists(f"wk:reply:{phone}:{body_hash}"))
        except (RedisError, OSError) as e:
            logger.warning("reply dedup Redis error — dict fallback: %s", e)
    prev_ts = _RECENT_REPLIES.get((phone, body_hash))
    return prev_ts is not None and (time.time() - prev_ts) < _REPLY_DEDUP_SECONDS


async def _record_reply_sent(phone: str, body_hash: str, now: float) -> None:
    """Record a successful outbound reply for dedup + ACK suppression."""
    r = get_redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.set(f"wk:reply:{phone}:{body_hash}", f"{now}", ex=int(_REPLY_DEDUP_SECONDS))
            # Per-phone "last real reply" timestamp — read by _send_ack to
            # suppress the redundant ACK on fast turns.
            pipe.set(f"wk:lastreply:{phone}", f"{now}", ex=int(_REPLY_DEDUP_SECONDS))
            await pipe.execute()
            return
        except (RedisError, OSError) as e:
            logger.warning("record_reply Redis error — dict fallback: %s", e)
    _RECENT_REPLIES[(phone, body_hash)] = now
    # Opportunistic GC so the dict doesn't grow without bound.
    if len(_RECENT_REPLIES) > 10_000:
        cleanup_recent_replies()


async def _real_reply_sent_since(phone: str, since_ts: float) -> bool:
    """Did the agent's real reply go out for this phone after ``since_ts``?"""
    r = get_redis()
    if r is not None:
        try:
            val = await r.get(f"wk:lastreply:{phone}")
            return bool(val) and float(val) >= since_ts
        except (RedisError, OSError, ValueError) as e:
            logger.warning("lastreply Redis error — dict fallback: %s", e)
    return any(
        p == phone and ts >= since_ts
        for (p, _h), ts in _RECENT_REPLIES.items()
    )


async def _send_reply(phone: str, response: str) -> None:
    """Send reply via WhatsApp (called by debouncer).

    Two dedupe guards layered on top of the send call:
      1. Suppress if the reply equals the last ACK (same content collision).
      2. Suppress if an identical (phone, body) pair was sent in the last
         ``_REPLY_DEDUP_SECONDS`` — protects against Meta webhook retries or
         duplicate agent turns producing the same payment link twice.

    The dedup cache is shared via Redis when configured (cross-worker safe);
    otherwise process-local. Full cross-restart duplicate protection would
    require a persisted response hash; this layer covers the dominant
    retry window.
    """
    if not response:
        return
    last = await _ack_get(phone)
    if last is not None:
        _, last_text = last
        if response.strip() == last_text.strip():
            logger.info("[%s] reply == ack, suppressing duplicate", mask_phone(phone))
            return

    _, body_hash = _reply_key(phone, response)
    now = time.time()
    if await _reply_recently_sent(phone, body_hash):
        logger.info(
            "[%s] suppressing duplicate outbound (same body within %.0fs)",
            mask_phone(phone), _REPLY_DEDUP_SECONDS,
        )
        metrics.inc("outbound_dedup_skip")
        return

    try:
        await whatsapp_service.send_text(phone, response)
        # Record only on successful send — failures should be retryable.
        await _record_reply_sent(phone, body_hash, now)
    except Exception as e:
        logger.error("Error sending WhatsApp reply to %s: %s", mask_phone(phone), e)
        await _record_failed_outbound(phone, response, "reply", str(e))


def get_ack(text: str) -> str:
    """Instant, rule-based acknowledgement. No LLM, <5ms.

    Sent immediately after the webhook receives a non-fast-path message so the
    user sees something in <300 ms while the main agent does its work.
    """
    text_lower = text.lower()

    urls = extract_urls(text)
    if any(is_product_link(u) for u in urls):
        return "Adding it to your wishlist 👍"

    if is_rule_based_affirmative(text):
        return "Got it 👍"

    if "gift" in text_lower:
        return "Got it — let me suggest something 🎁"

    return "Got it 👍"


_MIXED_INTENT_NUDGE = "Want help choosing a gift too? 🎁"


def _has_extra_text_beyond_links(text: str, urls: list[str]) -> bool:
    """True if the message has meaningful (>2 chars) text beyond the URL(s).

    Used to detect mixed-intent messages like "save this <url> what should i
    gift rahul" so we can append a follow-up nudge without calling the LLM.
    """
    if not urls:
        return False
    remainder = text
    for u in sorted(urls, key=len, reverse=True):
        remainder = remainder.replace(u, " ")
    remainder = re.sub(r"\s+", " ", remainder).strip()
    return len(remainder) > 2


def should_send_ack(text: str) -> bool:
    """Return True only for messages likely to benefit from an instant ACK."""
    t = (text or "").lower().strip()

    # Short conversational turns usually feel better with a direct reply.
    if len(t) <= 15:
        return False

    greetings = ["hi", "hello", "hey", "hii", "yo"]
    if any(t.startswith(g) for g in greetings):
        return False

    small_talk = [
        "how are you",
        "what's up",
        "how u doing",
        "kaise ho",
    ]
    if any(s in t for s in small_talk):
        return False

    return True


async def _run_fast_router(phone: str, message_text: str) -> str | None:
    """Try to handle the message via the deterministic pre-LLM router.

    Uses a fresh, short-lived DB session — never reuses a request-scoped one.
    Returns the reply text if handled, else None.
    """
    async with async_session() as db:
        try:
            reply = await try_handle_product_urls_before_classifier(
                db, phone, message_text
            )
            if reply is not None:
                await db.commit()
                return reply
            await db.rollback()
            return None
        except Exception as e:
            logger.error(
                "fast-router error for %s: %s", mask_phone(phone), e, exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return None


# Delay before the ACK actually leaves the webhook. If the real reply lands
# inside this window (captured in `_RECENT_REPLIES`), the ACK is suppressed —
# fast turns no longer show a redundant "Got it 👍" ahead of the real answer.
# Sized so slow turns still get their sub-1s perceived-latency buffer.
_ACK_SUPPRESS_DELAY_SECONDS = 0.8


async def _send_ack(phone: str, text: str) -> None:
    """Fire-and-forget ACK (cooldown-gated + delay-suppressed).

    Behaviour:
      1. Cooldown — don't re-ACK the same phone inside ``_ACK_COOLDOWN_SECONDS``.
      2. Delay — wait ``_ACK_SUPPRESS_DELAY_SECONDS`` before actually sending.
         If the real agent reply arrived during the wait (recorded in
         ``_RECENT_REPLIES`` by ``_send_reply``), skip the ACK entirely.
         Kills the visible "Got it 👍 / real reply" noise on fast turns
         without compromising the perceived-latency guarantee on slow ones.
    """
    now = time.time()
    last = await _ack_get(phone)
    if last is not None and (now - last[0]) < _ACK_COOLDOWN_SECONDS:
        return  # Already ACKed this burst.

    # Reserve the slot now so a parallel webhook for the same phone doesn't
    # also race into the ACK path.
    ack_text = get_ack(text)
    await _ack_set(phone, now, ack_text)

    # Wait the suppression window, then check whether the real reply already
    # went out for this phone. Any reply recorded since `now` indicates the
    # agent beat us — suppress.
    try:
        await asyncio.sleep(_ACK_SUPPRESS_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    if await _real_reply_sent_since(phone, now):
        metrics.inc("ack_suppressed_fast_turn")
        logger.debug("[%s] suppressing ACK — real reply already sent", mask_phone(phone))
        # Roll back the ACK slot so a follow-up message that DOES need
        # an ACK isn't blocked by the one we chose to skip.
        await _ack_rollback(phone, now)
        return

    try:
        await whatsapp_service.send_text(phone, ack_text)
    except Exception as e:
        logger.error("Failed to send ACK to %s: %s", mask_phone(phone), e)
        await _record_failed_outbound(phone, ack_text, "ack", str(e))


_FINAL_FALLBACK_TEXT = "Sorry, something went wrong. Try again 🙏"


async def _emit_fallback(phone: str) -> None:
    """Send the canned final fallback. Suppresses its own errors."""
    try:
        await whatsapp_service.send_text(phone, _FINAL_FALLBACK_TEXT)
    except Exception as e:
        logger.exception("Also failed to send fallback to %s", phone)
        await _record_failed_outbound(phone, _FINAL_FALLBACK_TEXT, "fallback", str(e))


async def _process_and_reply(phone: str, text: str, wamid: str = "") -> None:
    """Background worker — route the message through the existing pipeline.

    Uses the debouncer to preserve burst-message batching (3 rapid messages
    still become ONE agent turn). `add_message` returns the actual debouncer
    timer task; we register it in `_active_tasks` so it (a) counts toward the
    backpressure cap and (b) is cancelled cleanly on shutdown. We then
    `await` it under `_BACKGROUND_TIMEOUT_SECONDS` so a stuck Gemini call
    fires the canned fallback instead of hanging silently. The debouncer
    ultimately calls `_process_message`, which opens its OWN fresh
    `async_session()` per turn so background work never shares a session.
    """
    started = time.time()
    masked = mask_phone(phone)
    try:
        debounce_task = await message_debouncer.add_message(
            phone=phone,
            message=text,
            handler=_process_message,
            reply_callback=_send_reply,
        )
    except Exception as e:
        # `add_message` itself failed (lock issues / bad state) — surface a
        # fallback so the user isn't left hanging.
        metrics.inc("bg_failures")
        logger.error(
            "debouncer.add_message failed for %s: %s", masked, e, exc_info=True,
        )
        await _emit_fallback(phone)
        await _mark_processed_failed(wamid, "debouncer_add", type(e).__name__)
        return

    _register_task(debounce_task)

    try:
        # `shield` keeps the debounce task alive even if our wait_for is
        # cancelled by an outer caller — the inner work is already useful
        # (the eventual reply will still ship via `_send_reply`).
        await asyncio.wait_for(
            asyncio.shield(debounce_task), timeout=_BACKGROUND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        # Debouncer cancelled this run because a NEWER message arrived for
        # the same phone — that newer call's `_process_and_reply` will
        # produce the reply. Exit silently; sending a fallback here would
        # double-message the user.
        metrics.inc("bg_superseded")
        logger.debug("[%s] debouncer task superseded by newer message", masked)
        return
    except asyncio.TimeoutError:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("bg_timeouts")
        logger.warning(
            "latency",
            extra={
                "type": "bg_timeout",
                "phone": masked,
                "duration_ms": elapsed_ms,
                "bucket": latency_bucket(elapsed_ms),
            },
        )
        # Cancel the underlying task so resources are released; suppress
        # CancelledError from the cancellation we just initiated.
        debounce_task.cancel()
        try:
            await debounce_task
        except (asyncio.CancelledError, Exception):
            pass
        await _emit_fallback(phone)
        await _mark_processed_failed(wamid, "background", "TimeoutError")
    except Exception as e:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("bg_failures")
        logger.error(
            "Background process failed for %s after %.0fms: %s",
            masked, elapsed_ms, e, exc_info=True,
        )
        await _emit_fallback(phone)
        await _mark_processed_failed(wamid, "background", type(e).__name__)
    else:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("bg_completed")
        await _mark_processed_done(wamid)
        logger.info(
            "latency",
            extra={
                "type": "slow",
                "phone": masked,
                "duration_ms": elapsed_ms,
                "bucket": latency_bucket(elapsed_ms),
            },
        )


async def _process_immediate_and_reply(phone: str, text: str, wamid: str = "") -> None:
    """Inline no-ACK path for short, conversational turns.

    Runs the message pipeline exactly once and sends the final reply directly,
    skipping the debouncer so greetings and small-talk feel instant.
    """
    started = time.time()
    masked = mask_phone(phone)
    try:
        response = await asyncio.wait_for(
            _process_message(phone, text), timeout=_BACKGROUND_TIMEOUT_SECONDS,
        )
        await _send_reply(phone, response)
    except asyncio.TimeoutError:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("immediate_timeouts")
        logger.warning(
            "latency",
            extra={
                "type": "immediate_timeout",
                "phone": masked,
                "duration_ms": elapsed_ms,
                "bucket": latency_bucket(elapsed_ms),
            },
        )
        await _emit_fallback(phone)
        await _mark_processed_failed(wamid, "immediate", "TimeoutError")
    except Exception as e:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("immediate_failures")
        logger.error(
            "Immediate process failed for %s after %.0fms: %s",
            masked, elapsed_ms, e, exc_info=True,
        )
        await _emit_fallback(phone)
        await _mark_processed_failed(wamid, "immediate", type(e).__name__)
    else:
        elapsed_ms = (time.time() - started) * 1000
        metrics.inc("immediate_completed")
        await _mark_processed_done(wamid)
        logger.info(
            "latency",
            extra={
                "type": "immediate",
                "phone": masked,
                "duration_ms": elapsed_ms,
                "bucket": latency_bucket(elapsed_ms),
            },
        )


def _log_status_updates(statuses: list[dict]) -> None:
    """Log Meta delivery status callbacks without exposing full phone numbers."""
    for status_update in statuses:
        status = status_update.get("status") or "unknown"
        wamid = status_update.get("id") or ""
        phone = status_update.get("recipient_id") or ""
        errors = status_update.get("errors") or []

        if errors or status == "failed":
            for error in errors or [{}]:
                logger.warning(
                    "WhatsApp status failed phone=%s status=%s wamid=%s code=%s title=%s message=%s details=%s",
                    mask_phone(phone),
                    status,
                    wamid,
                    error.get("code"),
                    error.get("title"),
                    error.get("message"),
                    error.get("error_data", {}).get("details"),
                )
            continue

        conversation = status_update.get("conversation") or {}
        pricing = status_update.get("pricing") or {}
        logger.info(
            "WhatsApp status update phone=%s status=%s wamid=%s conversation=%s pricing_category=%s",
            mask_phone(phone),
            status,
            wamid,
            conversation.get("id"),
            pricing.get("category"),
        )


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Receive incoming WhatsApp messages from Meta Cloud API.
    Payload docs: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
    """
    client_ip = request.client.host if request.client else "unknown"
    raw_body = await request.body()

    # Never rate-limit valid Meta webhook traffic. Only apply the IP limiter to
    # traffic that fails signature validation (or cannot be validated in dev).
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _has_valid_meta_signature(raw_body, signature):
        if not await webhook_rate_limiter.allow(client_ip):
            logger.warning("Webhook rate limit exceeded for invalid signature IP %s", client_ip)
            raise HTTPException(status_code=429, detail="Too many requests")
        if settings.whatsapp_webhook_secret:
            logger.warning("Invalid webhook signature from %s", client_ip)
            raise HTTPException(status_code=403, detail="Invalid signature")

    body = json.loads(raw_body)

    try:
        # Meta sends: { "entry": [{ "changes": [{ "value": { "messages": [...] } }] }] }
        entries = body.get("entry", [])

        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                statuses = value.get("statuses", [])
                if statuses:
                    _log_status_updates(statuses)

                messages = value.get("messages", [])

                for msg in messages:
                    # Meta retries webhooks — dedupe on the wamid so we don't
                    # charge / reply / persist the same message twice.
                    wamid = msg.get("id", "")
                    if not await _claim_processed_message(wamid):
                        logger.info("Skipping duplicate wamid=%s", wamid[:32])
                        continue

                    phone = msg.get("from", "")
                    # Meta sends phone in format like "919876543210" (no +)
                    phone = "".join(c for c in phone if c.isdigit())
                    if len(phone) == 10:
                        phone = "91" + phone

                    if not phone:
                        continue

                    # Extract message text based on type
                    msg_type = msg.get("type", "")
                    message_text = ""

                    if msg_type == "text":
                        message_text = msg.get("text", {}).get("body", "")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        itype = interactive.get("type", "")
                        if itype == "button_reply":
                            message_text = interactive.get("button_reply", {}).get("title", "")
                        elif itype == "list_reply":
                            message_text = interactive.get("list_reply", {}).get("title", "")
                    elif msg_type == "button":
                        message_text = msg.get("button", {}).get("text", "")
                    elif msg_type == "contacts":
                        # Parse shared contacts and format for AI
                        try:
                            contacts = msg.get("contacts", [])
                            logger.info("Received %d shared contacts from %s", len(contacts), phone)
                            contact_list = []
                            for contact in contacts:
                                try:
                                    name = contact.get("name", {}).get("formatted_name") or "Unknown"
                                    phones = contact.get("phones", [])
                                    if phones:
                                        # Prefer wa_id (WhatsApp ID, clean format) over phone (may have formatting)
                                        phone_data = phones[0]
                                        raw_phone = str(phone_data.get("wa_id") or phone_data.get("phone") or "")
                                        clean_phone = "".join(c for c in raw_phone if c.isdigit())
                                        # Normalize to 12-digit Indian format
                                        if len(clean_phone) == 10:
                                            clean_phone = "91" + clean_phone
                                        elif len(clean_phone) == 11 and clean_phone.startswith("0"):
                                            clean_phone = "91" + clean_phone[1:]
                                        elif len(clean_phone) == 13 and clean_phone.startswith("91"):
                                            clean_phone = clean_phone[:12]
                                        if clean_phone and len(clean_phone) >= 10:
                                            contact_list.append(f"{name} ({clean_phone})")
                                except Exception as ce:
                                    logger.error("Error parsing contact: %s", ce)
                            if contact_list:
                                message_text = "[Shared contacts: " + ", ".join(contact_list) + "]"
                                logger.info(
                                    "Parsed %d contacts from %s",
                                    len(contact_list), mask_phone(phone),
                                )
                            else:
                                logger.warning(
                                    "No valid phone numbers found in contacts from %s",
                                    mask_phone(phone),
                                )
                        except Exception as e:
                            logger.error("Error parsing contacts message: %s", e, exc_info=True)

                    if not message_text:
                        logger.info(
                            "Skipping non-text message type=%s from %s",
                            msg_type, mask_phone(phone),
                        )
                        continue

                    metrics.inc("total_requests")

                    # Rate limit per phone number (protects Gemini API costs)
                    if not await message_rate_limiter.allow(phone):
                        metrics.inc("rate_limited")
                        logger.warning("Rate limit exceeded for phone %s", mask_phone(phone))
                        continue  # Silently drop — don't reveal rate limiting to potential attacker

                    # Burst guard — catches loops / double-tap floods in a
                    # tighter window than the broader per-minute limit.
                    if not await _burst_rate_limiter.allow(phone):
                        metrics.inc("burst_limited")
                        logger.warning("Burst limit exceeded for phone %s", mask_phone(phone))
                        continue

                    # Cap message length (prevents token burn attacks)
                    if len(message_text) > settings.max_message_length:
                        message_text = message_text[:settings.max_message_length]
                        logger.info(
                            "Truncated long message from %s (%d chars)",
                            mask_phone(phone), len(message_text),
                        )

                    # PII-safe inbound log — only the length is recorded, not
                    # the message body.
                    logger.info(
                        "inbound msg phone=%s len=%d type=%s",
                        mask_phone(phone), len(message_text), msg_type,
                    )

                    # Stamp last_inbound_at in its own commit before any
                    # handling runs. Covers both fast and slow paths and
                    # survives a handler-side rollback.
                    await _stamp_last_inbound(phone)

                    # ---- Fast path ------------------------------------------------
                    # Product-URL preflight is deterministic, cheap (<200 ms) and
                    # does not need an ACK — the user sees the canned save-reply
                    # directly. The path NEVER touches the classifier, Gemini,
                    # or the debouncer (audited: `_run_fast_router` only calls
                    # `try_handle_product_urls_before_classifier`).
                    t0 = time.time()
                    fast_reply = await _run_fast_router(phone, message_text)
                    if fast_reply is not None:
                        elapsed_ms = (time.time() - t0) * 1000
                        metrics.inc("fast_path_hits")
                        # Mixed-intent nudge: only when the save actually
                        # succeeded AND the user wrote more than just the URL.
                        if fast_reply == WISHLIST_ADDED_RESPONSE:
                            urls = extract_urls(message_text)
                            if _has_extra_text_beyond_links(message_text, urls):
                                fast_reply = f"{fast_reply}\n\n{_MIXED_INTENT_NUDGE}"
                        logger.info(
                            "[FAST_PATH] no LLM used | phone=%s elapsed=%.0fms",
                            mask_phone(phone), elapsed_ms,
                        )
                        logger.info(
                            "latency",
                            extra={
                                "type": "fast",
                                "phone": mask_phone(phone),
                                "duration_ms": elapsed_ms,
                                "bucket": latency_bucket(elapsed_ms),
                            },
                        )
                        # One transient retry: the wamid is already in the
                        # dedup LRU at this point, so a Meta retry will be
                        # silently dropped and the user would never see the
                        # confirmation. A short retry covers Meta's typical
                        # 5xx blip without queue infra.
                        for attempt in range(2):
                            try:
                                await whatsapp_service.send_text(phone, fast_reply)
                                await _mark_processed_done(wamid)
                                break
                            except Exception as e:
                                if attempt == 0:
                                    logger.warning(
                                        "Fast reply send failed for %s, retrying: %s",
                                        mask_phone(phone), e,
                                    )
                                    metrics.inc("fast_send_retries")
                                    await asyncio.sleep(0.25)
                                    continue
                                metrics.inc("fast_send_failures")
                                logger.error(
                                    "Failed to send fast reply to %s after retry: %s",
                                    mask_phone(phone), e,
                                )
                                await _record_failed_outbound(phone, fast_reply, "fast_reply", str(e))
                                await _mark_processed_done(wamid)
                        continue

                    # ---- Backpressure --------------------------------------------
                    # Fast path didn't take it — we're about to spawn a slow-path
                    # task. If we're already at the active-task ceiling, shed load
                    # *before* sending an ACK so the user sees one explicit
                    # "try again" message instead of an ACK with no follow-up.
                    if len(_active_tasks) >= MAX_ACTIVE_TASKS:
                        metrics.inc("backpressure_drops")
                        logger.warning(
                            "Active-task cap (%d) hit, shedding load for %s",
                            MAX_ACTIVE_TASKS, mask_phone(phone),
                        )
                        try:
                            await asyncio.wait_for(
                                whatsapp_service.send_text(phone, _BACKPRESSURE_TEXT),
                                timeout=_ACK_SEND_TIMEOUT_SECONDS,
                            )
                        except Exception as e:
                            logger.warning(
                                "Backpressure send failed for %s: %s",
                                mask_phone(phone), e,
                            )
                        continue

                    # ---- Slow path -----------------------------------------------
                    # ACK first (awaited, with a strict 0.5 s ceiling) so it
                    # ALWAYS lands before the final reply — and never delays the
                    # webhook past Meta's 5 s response budget. The heavy work
                    # is then handed to a tracked background task.
                    if should_send_ack(message_text):
                        metrics.inc("slow_path_dispatched")
                        ack_t0 = time.time()
                        try:
                            await asyncio.wait_for(
                                _send_ack(phone, message_text),
                                timeout=_ACK_SEND_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            metrics.inc("ack_timeouts")
                            logger.warning(
                                "ACK send timed out for %s after %.2fs — continuing",
                                mask_phone(phone), _ACK_SEND_TIMEOUT_SECONDS,
                            )
                        except Exception as e:
                            metrics.inc("ack_failures")
                            logger.warning(
                                "ACK send failed for %s: %s — continuing",
                                mask_phone(phone), e,
                            )
                        finally:
                            ack_ms = (time.time() - ack_t0) * 1000
                            logger.info(
                                "latency",
                                extra={
                                    "type": "ack",
                                    "phone": mask_phone(phone),
                                    "duration_ms": ack_ms,
                                    "bucket": latency_bucket(ack_ms),
                                },
                            )

                        _spawn_tracked(_process_and_reply(phone, message_text, wamid))
                    else:
                        metrics.inc("immediate_path_dispatched")
                        await _process_immediate_and_reply(phone, message_text, wamid)

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing WhatsApp webhook: %s", e, exc_info=True)
        return {"status": "error"}


@router.get("/whatsapp/_metrics")
async def webhook_metrics() -> dict:
    """Lightweight in-memory counters for observability.

    Not a Prometheus endpoint — exposes process-local counters useful for
    quick `curl` health checks. Production scrapers should replace
    `app.utils.observability.metrics` with a real client.
    """
    if settings.app_env == "production":
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "active_tasks": len(_active_tasks),
        "max_active_tasks": MAX_ACTIVE_TASKS,
        "counters": metrics.snapshot(),
    }
