"""Transient database disconnect retry utilities.

Provides safe retry logic for transient DB errors (connection closed, server
restart, network blip) without hiding real bugs or duplicating side effects.

Usage:
    # As a wrapper
    result = await retry_db_operation(
        lambda: db.execute(select(User).where(User.id == uid)),
        label="load_user",
    )

    # As a decorator
    @with_db_retry(label="fetch_orders")
    async def fetch_orders(db, user_id):
        return await db.execute(...)

Safety rules:
- Only retries transient connection errors, not integrity/logic errors
- Max 1 retry by default (2 total attempts)
- Does NOT retry external side effects (WhatsApp, Razorpay, Hubble)
- Logs all retry attempts for observability
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)

logger = logging.getLogger("wishkr.db_retry")

T = TypeVar("T")

_TRANSIENT_PATTERNS = (
    "connection is closed",
    "server closed the connection unexpectedly",
    "connection refused",
    "connection reset",
    "connection was closed",
    "terminating connection",
    "ssl connection has been closed unexpectedly",
    "could not connect to server",
    "network is unreachable",
    "no route to host",
    "connection timed out",
    "broken pipe",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """Return True if the exception indicates a transient DB disconnect.

    Transient errors are safe to retry because they indicate the connection
    died mid-request, not a bug in the query or data.

    We explicitly do NOT retry:
    - IntegrityError (constraint violations, duplicate keys)
    - ProgrammingError (SQL syntax, missing tables)
    - DataError (invalid data types)
    - Any error without a transient message pattern
    """
    if isinstance(exc, (IntegrityError, ProgrammingError)):
        return False

    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        return True

    if isinstance(exc, OSError):
        msg = str(exc).lower()
        if any(p in msg for p in ("connection refused", "connection reset", "broken pipe")):
            return True

    if isinstance(exc, (InterfaceError, OperationalError)):
        msg = str(exc).lower()
        if any(pattern in msg for pattern in _TRANSIENT_PATTERNS):
            return True

    if isinstance(exc, DBAPIError):
        orig = getattr(exc, "orig", None)
        if orig is not None:
            orig_msg = str(orig).lower()
            if any(pattern in orig_msg for pattern in _TRANSIENT_PATTERNS):
                return True

    return False


async def retry_db_operation(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 2,
    label: str = "",
    backoff_seconds: float = 0.1,
) -> T:
    """Execute an async DB operation with transient-error retry.

    Args:
        fn: Zero-argument async callable that performs the DB operation.
        max_attempts: Total attempts (default 2 = 1 retry).
        label: Human-readable label for logging.
        backoff_seconds: Sleep between retries.

    Returns:
        The result of fn() on success.

    Raises:
        The original exception if all attempts fail or error is non-transient.

    Example:
        result = await retry_db_operation(
            lambda: db.scalar(select(User).where(User.id == uid)),
            label="load_user",
        )
    """
    last_exc: BaseException | None = None
    tag = f"[{label}]" if label else ""

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc

            if not is_transient_db_error(exc):
                raise

            if attempt >= max_attempts:
                logger.warning(
                    "%s DB operation failed after %d attempts: %s",
                    tag,
                    max_attempts,
                    exc,
                )
                raise

            logger.info(
                "%s Transient DB error on attempt %d/%d, retrying: %s",
                tag,
                attempt,
                max_attempts,
                type(exc).__name__,
            )
            await asyncio.sleep(backoff_seconds)

    assert last_exc is not None
    raise last_exc


def with_db_retry(
    label: str = "",
    max_attempts: int = 2,
    backoff_seconds: float = 0.1,
):
    """Decorator to wrap an async function with transient DB retry.

    Usage:
        @with_db_retry(label="fetch_user")
        async def fetch_user(db, user_id):
            return await db.scalar(select(User).where(User.id == user_id))

    The decorated function will retry once on transient DB errors.
    Non-transient errors (IntegrityError, ProgrammingError) are raised immediately.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await retry_db_operation(
                lambda: fn(*args, **kwargs),
                max_attempts=max_attempts,
                label=label or fn.__name__,
                backoff_seconds=backoff_seconds,
            )

        return wrapper

    return decorator
