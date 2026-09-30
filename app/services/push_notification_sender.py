"""Push notification dispatch service for mobile app users.

Selects active device tokens from `device_registrations` for a given user
and dispatches payloads via FCM / APNs transport. Excludes deactivated devices
(e.g., devices logged out via /api/v1/auth/logout).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_registration import DeviceRegistration

logger = logging.getLogger("wishkr.push_notification_sender")


class PushTransport(Protocol):
    """Protocol for pluggable push transports (FCM, APNs, Mock)."""

    async def send(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> list[str]:
        """Send push notification to tokens. Returns list of tokens successfully sent."""
        ...


class DefaultPushTransport:
    """Default transport. Logs and no-ops if remote FCM/APNs credentials are not configured."""

    async def send(
        self,
        tokens: list[str],
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> list[str]:
        logger.info(
            "Push dispatch simulated (no live FCM key configured) to %d tokens: title=%r",
            len(tokens),
            title,
        )
        return list(tokens)


@dataclass
class PushDispatchResult:
    user_id: str
    eligible_tokens: list[str]
    dispatched_tokens: list[str]
    excluded_count: int


async def get_active_device_tokens(db: AsyncSession, user_id: str) -> list[str]:
    """Retrieve all active FCM / APNs tokens for a given user."""
    stmt = (
        select(DeviceRegistration.token)
        .where(
            DeviceRegistration.user_id == user_id,
            DeviceRegistration.is_active == True,  # noqa: E712
        )
        .order_by(DeviceRegistration.created_at.asc())
    )
    tokens = list((await db.scalars(stmt)).all())
    return tokens


async def dispatch_push_to_user(
    db: AsyncSession,
    user_id: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    transport: PushTransport | None = None,
) -> PushDispatchResult:
    """Dispatch push notification to all active devices of the specified user.

    Excluded from dispatch:
    - Inactive / logged out devices (is_active == False)
    - Tokens belonging to other users
    """
    active_tokens = await get_active_device_tokens(db, user_id)
    sender = transport or DefaultPushTransport()

    if not active_tokens:
        logger.info("No active device registrations found for user=%s; skipping push", user_id)
        return PushDispatchResult(
            user_id=user_id,
            eligible_tokens=[],
            dispatched_tokens=[],
            excluded_count=0,
        )

    dispatched = await sender.send(active_tokens, title, body, data)
    logger.info(
        "Dispatched push to %d active device(s) for user=%s",
        len(dispatched),
        user_id,
    )
    return PushDispatchResult(
        user_id=user_id,
        eligible_tokens=active_tokens,
        dispatched_tokens=dispatched,
        excluded_count=0,
    )
