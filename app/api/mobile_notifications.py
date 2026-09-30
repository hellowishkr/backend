"""Mobile Notifications & Device Token API routes for wishkr_app.

Endpoints:
  POST /api/v1/notifications/device-token — Register FCM / APNs device token
  GET  /api/v1/notifications              — Fetch in-app notifications
  POST /api/v1/notifications/{id}/read    — Mark notification as read
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mobile_auth import get_current_mobile_user
from app.db.session import get_db
from app.models.occasion import Occasion
from app.models.recipient_preference import PreferenceRequest
from app.models.user import User

logger = logging.getLogger("wishkr.mobile_notifications")

router = APIRouter(prefix="/v1/notifications", tags=["mobile_notifications"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class DeviceTokenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str = Field(
        ..., min_length=10, description="FCM registration token or APNs token string"
    )
    platform: str = Field("android", description="'android' or 'ios'")


class InAppNotificationItem(BaseModel):
    id: str
    title: str
    body: str
    type: str  # 'preference_request' | 'upcoming_celebration' | 'wishlist_reserved'
    created_at: str
    is_read: bool = False
    action_url: str | None = None


from app.models.device_registration import DeviceRegistration
from app.utils.id_gen import generate_id


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/device-token", status_code=status.HTTP_200_OK)
async def register_device_token(
    payload: DeviceTokenPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Store device token for push notification delivery via APNs / FCM in database."""
    now = datetime.now(timezone.utc)

    # 1. Device reassignment: if another user was previously using this token on this device,
    # mark their registration inactive so they don't receive this user's notifications.
    await db.execute(
        update(DeviceRegistration)
        .where(
            DeviceRegistration.token == payload.token,
            DeviceRegistration.user_id != user.id,
        )
        .values(is_active=False)
    )

    # 2. Find or create registration for this user
    stmt = select(DeviceRegistration).where(
        DeviceRegistration.user_id == user.id,
        DeviceRegistration.token == payload.token,
    )
    reg = await db.scalar(stmt)
    if reg:
        reg.is_active = True
        reg.platform = payload.platform.lower()
        reg.updated_at = now
    else:
        reg = DeviceRegistration(
            id=generate_id("dev"),
            user_id=user.id,
            token=payload.token,
            platform=payload.platform.lower(),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(reg)

    try:
        await db.commit()
    except IntegrityError:
        # Concurrent retry or simultaneous registration across accounts:
        # Roll back and ensure any other account's active registration on this device is deactivated
        await db.rollback()
        await db.execute(
            update(DeviceRegistration)
            .where(
                DeviceRegistration.token == payload.token,
                DeviceRegistration.user_id != user.id,
                DeviceRegistration.is_active.is_(True),
            )
            .values(is_active=False)
        )
        reg = await db.scalar(
            select(DeviceRegistration).where(
                DeviceRegistration.user_id == user.id,
                DeviceRegistration.token == payload.token,
            )
        )
        if reg:
            reg.is_active = True
            reg.platform = payload.platform.lower()
            reg.updated_at = now
        else:
            reg = DeviceRegistration(
                id=generate_id("dev"),
                user_id=user.id,
                token=payload.token,
                platform=payload.platform.lower(),
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(reg)
        await db.commit()

    logger.info(
        "Registered %s device token for user %s (%s...)",
        payload.platform,
        user.id,
        payload.token[:12],
    )
    return {"status": "ok", "message": "Device token registered"}


@router.get("", response_model=list[InAppNotificationItem])
async def list_notifications(
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InAppNotificationItem]:
    """Return in-app notifications (preference requests, celebrations approaching)."""
    now = datetime.now(timezone.utc)
    items: list[InAppNotificationItem] = []

    # 1. Check for incoming preference requests to this user's phone
    clean_phone = user.phone
    if clean_phone:
        stmt = (
            select(PreferenceRequest)
            .where(
                PreferenceRequest.recipient_phone.contains(clean_phone[-10:]),
                PreferenceRequest.status == "pending",
            )
            .order_by(PreferenceRequest.created_at.desc())
            .limit(5)
        )
        pref_reqs = list((await db.scalars(stmt)).all())
        for req in pref_reqs:
            items.append(
                InAppNotificationItem(
                    id=f"notif_prq_{req.id}",
                    title="Friend Asked for Your Gift Vibe! 🎁",
                    body="A friend wants to know what you want for your celebration. Tap to choose your vibes.",
                    type="preference_request",
                    created_at=req.created_at.isoformat() if req.created_at else now.isoformat(),
                    is_read=False,
                    action_url="/circle/preferences",
                )
            )

    # 2. Upcoming celebrations in next 7 days
    stmt_occ = (
        select(Occasion)
        .where(
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
        .order_by(Occasion.date.asc())
        .limit(3)
    )
    occasions = list((await db.scalars(stmt_occ)).all())
    for occ in occasions:
        items.append(
            InAppNotificationItem(
                id=f"notif_occ_{occ.id}",
                title="Celebration Approaching! 🎂",
                body=f"Upcoming celebration on {occ.date.strftime('%B %d')}. Don't forget to check gifts!",
                type="upcoming_celebration",
                created_at=now.isoformat(),
                is_read=False,
                action_url=f"/circle/occasion/{occ.id}",
            )
        )

    return items


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
) -> dict:
    """Acknowledge notification as read."""
    return {"status": "ok", "id": notification_id, "read": True}
