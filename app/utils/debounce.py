"""Message debouncer for WhatsApp — batches rapid-fire messages before processing.

Users often send multiple messages quickly. This waits for a pause before
combining them into a single request to the AI agent.

With ``REDIS_URL`` configured, the pending-message buffer and the debounce
deadline live in Redis, shared by all uvicorn workers: a burst from one
phone that load-balances across two workers still produces ONE combined
agent call. Every message spawns a local timer on the worker that received
it; after the delay, only the timer that successfully claims the deadline
key (GETDEL) processes the batch — the others see a missing/extended
deadline and exit. Without Redis, the original in-process implementation
is used (single-worker only).
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable

from redis.exceptions import RedisError

from app.core.config import settings
from app.utils.redis_client import get_redis

logger = logging.getLogger("wishkr.debounce")

# Buffer/deadline TTL — bounds orphaned state if a worker dies between
# claiming a batch and processing it (the next inbound from that phone
# re-creates the deadline and drains whatever survived).
_DEBOUNCE_STATE_TTL = 60


@dataclass
class PendingMessages:
    """Holds queued messages for a phone number."""
    messages: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    first_received: datetime = field(default_factory=datetime.utcnow)


class MessageDebouncer:
    """
    Debounces incoming messages per phone number.

    When a message arrives:
    1. Add it to the queue for that phone
    2. Cancel any existing timer
    3. Start a new timer (DEBOUNCE_DELAY seconds)
    4. When timer fires, combine all messages and call the handler
    """

    def __init__(self):
        self._pending: dict[str, PendingMessages] = defaultdict(PendingMessages)
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._registry_lock = asyncio.Lock()

    async def _get_phone_lock(self, phone: str) -> asyncio.Lock:
        # Get-or-create + ref increment must be atomic. The previous version
        # had a fast path that read ``_locks.get(phone)`` outside the registry
        # lock, then incremented inside — which interleaved with a concurrent
        # release that popped the lock from ``_locks``. The next caller would
        # create a *different* asyncio.Lock for the same phone, breaking
        # serialisation across in-flight messages and leaking the old refcount.
        async with self._registry_lock:
            lock = self._locks.get(phone)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[phone] = lock
            self._lock_refs[phone] = self._lock_refs.get(phone, 0) + 1
            return lock

    async def _release_phone_lock_ref(self, phone: str, lock: asyncio.Lock) -> None:
        async with self._registry_lock:
            current = self._locks.get(phone)
            if current is not lock:
                return
            remaining = self._lock_refs.get(phone, 0) - 1
            if remaining > 0:
                self._lock_refs[phone] = remaining
                return
            self._lock_refs.pop(phone, None)
            if phone not in self._pending:
                self._locks.pop(phone, None)

    async def add_message(
        self,
        phone: str,
        message: str,
        handler: Callable[[str, str], Awaitable[str]],
        reply_callback: Callable[[str, str], Awaitable[None]],
    ) -> asyncio.Task:
        """
        Add a message to the queue for this phone.

        Returns the task that will eventually run the combined handler so
        callers can `await`/timeout/track it. NOTE: when a newer message
        arrives for the same phone before the debounce delay elapses, this
        task is cancelled and a fresh one is started — callers awaiting the
        old task will see `asyncio.CancelledError` and should treat that as
        "superseded by a newer message", not as an error.

        Args:
            phone: User's phone number
            message: The message text
            handler: Async function(phone, combined_text) -> response
            reply_callback: Async function(phone, response) to send the reply
        """
        r = get_redis()
        if r is not None:
            try:
                return await self._add_message_redis(
                    r, phone, message, handler, reply_callback
                )
            except (RedisError, OSError) as e:
                logger.warning("Debounce Redis error — in-process fallback: %s", e)

        lock = await self._get_phone_lock(phone)
        old_task: asyncio.Task | None = None
        try:
            async with lock:
                pending = self._pending[phone]

                # Cancel existing timer if any
                if pending.task and not pending.task.done():
                    old_task = pending.task
                    old_task.cancel()

                # Add message to queue
                pending.messages.append(message)
                logger.debug("Queued message for %s (total: %d)", phone, len(pending.messages))

                # Start new timer
                pending.task = asyncio.create_task(
                    self._process_after_delay(phone, handler, reply_callback)
                )
                new_task = pending.task

            if old_task is not None:
                try:
                    await old_task
                except asyncio.CancelledError:
                    pass

            return new_task
        finally:
            await self._release_phone_lock_ref(phone, lock)

    async def _process_after_delay(
        self,
        phone: str,
        handler: Callable[[str, str], Awaitable[str]],
        reply_callback: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Wait for the debounce delay, then process all queued messages."""
        lock: asyncio.Lock | None = None
        try:
            await asyncio.sleep(settings.message_debounce_seconds)

            lock = await self._get_phone_lock(phone)
            async with lock:
                pending = self._pending.pop(phone, None)
                if not pending or not pending.messages:
                    return

                messages = pending.messages

            await self._run_handler(phone, messages, handler, reply_callback)

        except asyncio.CancelledError:
            # Timer was cancelled because a newer message arrived (or the
            # process is shutting down). Re-raise so callers awaiting this
            # task can distinguish "superseded" from "completed normally";
            # `add_message` already swallows CancelledError on its own
            # awaits, so internal flow is unaffected.
            raise
        finally:
            if lock is not None:
                await self._release_phone_lock_ref(phone, lock)

    async def _run_handler(
        self,
        phone: str,
        messages: list[str],
        handler: Callable[[str, str], Awaitable[str]],
        reply_callback: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Combine a drained batch and run handler + reply (both backends)."""
        if len(messages) == 1:
            combined = messages[0]
        else:
            combined = "\n".join(messages)
            logger.info("Combined %d messages for %s", len(messages), phone)

        try:
            response = await handler(phone, combined)
            await reply_callback(phone, response)
        except Exception as e:
            logger.error("Error processing debounced messages for %s: %s", phone, e)
            await reply_callback(
                phone,
                "Sorry, something went wrong on my end. Please try again or type *menu* for options."
            )

    # ── Redis-coordinated path (cross-worker) ────────────────────────────

    @staticmethod
    def _redis_keys(phone: str) -> tuple[str, str]:
        return f"wk:debounce:m:{phone}", f"wk:debounce:d:{phone}"

    async def _add_message_redis(
        self,
        r,
        phone: str,
        message: str,
        handler: Callable[[str, str], Awaitable[str]],
        reply_callback: Callable[[str, str], Awaitable[None]],
    ) -> asyncio.Task:
        """Buffer the message in Redis and (re)arm the shared deadline.

        No cross-worker timer cancellation: each message spawns a timer on
        its receiving worker, and the deadline key arbitrates which timer
        actually processes. Earlier timers wake, see the deadline moved
        past their own, and exit.
        """
        msgs_key, deadline_key = self._redis_keys(phone)
        deadline = time.time() + settings.message_debounce_seconds

        pipe = r.pipeline()
        pipe.rpush(msgs_key, message)
        pipe.expire(msgs_key, _DEBOUNCE_STATE_TTL)
        pipe.set(deadline_key, f"{deadline}", ex=_DEBOUNCE_STATE_TTL)
        await pipe.execute()
        logger.debug("Queued message for %s in Redis (deadline=%s)", phone, deadline)

        return asyncio.create_task(
            self._process_redis_after_delay(phone, deadline, handler, reply_callback)
        )

    async def _process_redis_after_delay(
        self,
        phone: str,
        my_deadline: float,
        handler: Callable[[str, str], Awaitable[str]],
        reply_callback: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Wake after the delay; process the batch only if we own the deadline."""
        await asyncio.sleep(settings.message_debounce_seconds)
        msgs_key, deadline_key = self._redis_keys(phone)

        r = get_redis()
        if r is None:
            logger.warning("Debounce timer woke without Redis — batch for %s deferred", phone)
            return
        try:
            val = await r.get(deadline_key)
            if val is None:
                return  # Another timer already claimed this batch.
            if float(val) > my_deadline + 1e-3:
                return  # A newer message extended the deadline; its timer owns it.

            claimed = await r.getdel(deadline_key)
            if claimed is None:
                return  # Lost the claim race to a sibling timer.
            if float(claimed) > my_deadline + 1e-3:
                # Raced with a newer message between GET and GETDEL — we stole
                # its deadline. Restore it so the newer timer still fires.
                await r.set(deadline_key, claimed, ex=_DEBOUNCE_STATE_TTL)
                return

            # We own the batch. Drain with LPOP (element-atomic) so a message
            # RPUSHed mid-drain is never lost — it stays for its own timer.
            messages: list[str] = []
            while (m := await r.lpop(msgs_key)) is not None:
                messages.append(m)
        except (RedisError, OSError, ValueError) as e:
            logger.warning("Debounce claim/drain Redis error for %s: %s", phone, e)
            return

        if not messages:
            return

        await self._run_handler(phone, messages, handler, reply_callback)


# Global debouncer instance
message_debouncer = MessageDebouncer()
