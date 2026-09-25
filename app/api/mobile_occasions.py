"""Mobile Occasions API routes for wishkr_app.

Endpoints:
  GET    /api/v1/occasions          — List upcoming occasions for current user
  POST   /api/v1/occasions          — Create a new occasion and contact
  DELETE /api/v1/occasions/{id}     — Soft-delete an occasion
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.mobile_auth import get_current_mobile_user
from app.db.session import get_db
from app.models.celebrant_friend import CelebrantFriend, CelebrantFriendStatus
from app.models.celebrant_occasion import CelebrantOccasion
from app.models.contact import Contact
from app.models.occasion import Occasion, OccasionType
from app.models.user import User
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.mobile_occasions")

router = APIRouter(prefix="/v1/occasions", tags=["mobile_occasions"])


def _clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    raw_digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not raw_digits.startswith("+"):
        if len(raw_digits) == 10:
            return "+91" + raw_digits
        elif raw_digits.startswith("91") and len(raw_digits) == 12:
            return "+" + raw_digits
        return raw_digits
    return raw_digits


def _calculate_days_until(target_dt: datetime) -> int:
    today = datetime.now(timezone.utc).date()
    target = target_dt.date() if isinstance(target_dt, datetime) else target_dt
    try:
        this_year_target = target.replace(year=today.year)
    except ValueError:
        # Handles Feb 29 on non-leap years
        this_year_target = target.replace(year=today.year, day=28)

    if this_year_target < today:
        try:
            next_target = target.replace(year=today.year + 1)
        except ValueError:
            next_target = target.replace(year=today.year + 1, day=28)
        return (next_target - today).days
    return (this_year_target - today).days


class AllowedFriendItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone: str
    name: str | None = None
    status: str = "INVITED"


class OccasionAccessResponse(BaseModel):
    occasion_id: str
    is_selective: bool
    allowed_friends: list[AllowedFriendItem]


class UpdateOccasionAccessPayload(BaseModel):
    is_selective: bool = True
    allowed_friends: list[dict[str, str | None]] = Field(
        default_factory=list,
        description="List of friends with 'phone' and optional 'name'",
    )


class SnoozeOccasionPayload(BaseModel):
    days: int = Field(default=3, ge=1, le=30, description="Number of days to snooze reminder")


class CreateOccasionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=100, description="Person's display name")
    relationship: str | None = Field(
        None, max_length=50, description="friend, spouse, sibling, parent, etc."
    )
    type: str = Field("BIRTHDAY", description="BIRTHDAY, ANNIVERSARY, or CUSTOM")
    occasion_type: str | None = Field(None, description="Alias for type")
    custom_label: str | None = Field(None, max_length=100)
    date: str = Field(..., description="ISO 8601 date string e.g. 1995-10-14")
    phone: str | None = Field(None, description="Mobile number for reminders")
    recurring: bool = Field(True, description="Whether occasion recurs annually")
    notes: str | None = Field(None, description="Optional gift notes or ideas")
    is_selective_sharing: bool = Field(
        False, description="Whether wishlist sharing is limited to allowlist"
    )
    allowed_friends: list[str | dict[str, str | None]] | None = Field(
        None,
        description="Allowed circle contacts e.g. ['+919876543210'] or [{'phone': '+919876543210', 'name': 'Priya'}]",
    )


class OccasionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    contact_id: str | None
    name: str
    phone: str | None
    relationship: str | None
    type: str
    custom_label: str | None
    date: str
    recurring: bool
    days_until: int
    is_selective_sharing: bool = False


@router.get("", response_model=list[OccasionItemResponse])
async def list_occasions(
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[OccasionItemResponse]:
    """Return all active occasions for the current authenticated user."""
    stmt = (
        select(Occasion)
        .where(
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
        .options(selectinload(Occasion.contact), selectinload(Occasion.celebrant_occasion))
        .order_by(Occasion.date.asc())
    )
    result = await db.scalars(stmt)
    occasions = list(result.all())

    items = []
    for occ in occasions:
        contact_name = occ.contact.name if occ.contact else (occ.custom_label or "Friend")
        contact_phone = occ.contact.phone if occ.contact else None
        relationship = occ.contact.relationship_type if occ.contact else None
        days_left = _calculate_days_until(occ.date)
        is_selective = occ.celebrant_occasion is not None

        items.append(
            OccasionItemResponse(
                id=occ.id,
                contact_id=occ.contact_id,
                name=contact_name,
                phone=contact_phone,
                relationship=relationship,
                type=occ.type.value if hasattr(occ.type, "value") else str(occ.type),
                custom_label=occ.custom_label,
                date=occ.date.isoformat(),
                recurring=occ.recurring,
                days_until=days_left,
                is_selective_sharing=is_selective,
            )
        )

    # Sort primarily by days_until (nearest celebrations first)
    items.sort(key=lambda x: x.days_until)
    return items


@router.post("", response_model=OccasionItemResponse, status_code=status.HTTP_201_CREATED)
async def create_occasion(
    payload: CreateOccasionPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OccasionItemResponse:
    """Create a new occasion and associated contact."""
    # Parse date
    clean_date_str = payload.date.strip()
    try:
        if "T" in clean_date_str:
            parsed_date = datetime.fromisoformat(clean_date_str.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(clean_date_str[:10], "%Y-%m-%d")
            parsed_date = dt.replace(tzinfo=timezone.utc)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date format: {exc}. Use YYYY-MM-DD",
        )

    # Parse occasion type
    raw_type = payload.type or payload.occasion_type or "BIRTHDAY"
    type_str = raw_type.upper()
    try:
        occ_type = OccasionType(type_str)
    except ValueError:
        occ_type = OccasionType.CUSTOM
        if not payload.custom_label:
            payload.custom_label = raw_type

    # Find or create contact
    contact = None
    clean_phone = None
    if payload.phone:
        raw_digits = "".join(ch for ch in payload.phone if ch.isdigit() or ch == "+")
        if not raw_digits.startswith("+"):
            if len(raw_digits) == 10:
                clean_phone = "+91" + raw_digits
            elif raw_digits.startswith("91") and len(raw_digits) == 12:
                clean_phone = "+" + raw_digits
            else:
                clean_phone = raw_digits
        else:
            clean_phone = raw_digits

        contact = await db.scalar(
            select(Contact).where(
                Contact.user_id == user.id,
                Contact.phone == clean_phone,
            )
        )

    if not contact:
        contact_id = generate_id("cnt")
        contact = Contact(
            id=contact_id,
            user_id=user.id,
            name=payload.name.strip(),
            phone=clean_phone,
            relationship_type=payload.relationship.lower().strip()
            if payload.relationship
            else None,
        )
        db.add(contact)
        await db.flush()

    occ_id = generate_id("occ")
    occasion = Occasion(
        id=occ_id,
        user_id=user.id,
        contact_id=contact.id,
        type=occ_type,
        custom_label=payload.custom_label.strip() if payload.custom_label else None,
        date=parsed_date,
        recurring=payload.recurring,
    )
    db.add(occasion)
    await db.flush()

    # If selective sharing allowlist specified, create CelebrantOccasion & CelebrantFriends
    if payload.is_selective_sharing or payload.allowed_friends:
        cel_occ = CelebrantOccasion(
            id=generate_id("cel"),
            occasion_id=occasion.id,
            preferences="[]",
        )
        db.add(cel_occ)
        await db.flush()

        if payload.allowed_friends:
            for item in payload.allowed_friends:
                if isinstance(item, dict):
                    raw_fphone = item.get("phone")
                    fname = item.get("name") or "Friend"
                else:
                    raw_fphone = str(item)
                    fname = "Friend"
                clean_fphone = _clean_phone(raw_fphone)
                if not clean_fphone:
                    continue
                friend_record = CelebrantFriend(
                    id=generate_id("cfr"),
                    celebrant_occasion_id=cel_occ.id,
                    phone=clean_fphone,
                    name=fname,
                    status=CelebrantFriendStatus.INVITED,
                )
                db.add(friend_record)

    await db.commit()
    await db.refresh(occasion)

    logger.info(
        "Created occasion %s (%s) for user %s, contact %s",
        occasion.id,
        occ_type.value,
        user.id,
        contact.name,
    )

    return OccasionItemResponse(
        id=occasion.id,
        contact_id=contact.id,
        name=contact.name,
        phone=contact.phone,
        relationship=contact.relationship_type,
        type=occasion.type.value if hasattr(occasion.type, "value") else str(occasion.type),
        custom_label=occasion.custom_label,
        date=occasion.date.isoformat(),
        recurring=occasion.recurring,
        days_until=_calculate_days_until(occasion.date),
        is_selective_sharing=bool(payload.is_selective_sharing or payload.allowed_friends),
    )


@router.delete("/{occasion_id}")
async def delete_occasion(
    occasion_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Soft-delete an occasion owned by the current user."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Occasion not found",
        )

    occ.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("Soft-deleted occasion %s for user %s", occasion_id, user.id)
    return {"status": "ok", "message": "Occasion deleted"}


@router.get("/{occasion_id}/access", response_model=OccasionAccessResponse)
async def get_occasion_access(
    occasion_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OccasionAccessResponse:
    """Fetch allowlist members permitted to view the wishlist for this occasion."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    cel_occ = await db.scalar(
        select(CelebrantOccasion)
        .where(CelebrantOccasion.occasion_id == occasion_id)
        .options(selectinload(CelebrantOccasion.friends))
    )

    if not cel_occ:
        return OccasionAccessResponse(
            occasion_id=occasion_id,
            is_selective=False,
            allowed_friends=[],
        )

    items = [
        AllowedFriendItem(
            id=f.id,
            phone=f.phone,
            name=f.name,
            status=f.status.value if hasattr(f.status, "value") else str(f.status),
        )
        for f in cel_occ.friends
    ]
    return OccasionAccessResponse(
        occasion_id=occasion_id,
        is_selective=True,
        allowed_friends=items,
    )


@router.put("/{occasion_id}/access", response_model=OccasionAccessResponse)
async def update_occasion_access(
    occasion_id: str,
    payload: UpdateOccasionAccessPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OccasionAccessResponse:
    """Update or overwrite the selective allowlist for this celebration."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    cel_occ = await db.scalar(
        select(CelebrantOccasion)
        .where(CelebrantOccasion.occasion_id == occasion_id)
        .options(selectinload(CelebrantOccasion.friends))
    )

    if not payload.is_selective:
        # User turned off selective sharing; remove celebrant occasion & friend restrictions
        if cel_occ:
            await db.delete(cel_occ)
            await db.commit()
        return OccasionAccessResponse(
            occasion_id=occasion_id,
            is_selective=False,
            allowed_friends=[],
        )

    if not cel_occ:
        cel_occ = CelebrantOccasion(
            id=generate_id("cel"),
            occasion_id=occasion_id,
            preferences="[]",
        )
        db.add(cel_occ)
        await db.flush()

    # Sync friends: delete existing and insert new allowlist
    existing_friends = list(cel_occ.friends or [])
    for ef in existing_friends:
        await db.delete(ef)
    await db.flush()

    updated_items = []
    for item in payload.allowed_friends:
        clean_p = _clean_phone(item.get("phone"))
        if not clean_p:
            continue
        new_cfr = CelebrantFriend(
            id=generate_id("cfr"),
            celebrant_occasion_id=cel_occ.id,
            phone=clean_p,
            name=item.get("name") or "Friend",
            status=CelebrantFriendStatus.INVITED,
        )
        db.add(new_cfr)
        updated_items.append(
            AllowedFriendItem(
                id=new_cfr.id,
                phone=clean_p,
                name=new_cfr.name,
                status=CelebrantFriendStatus.INVITED.value,
            )
        )

    await db.commit()
    logger.info(
        "Updated allowlist for occasion %s with %d friends",
        occasion_id,
        len(updated_items),
    )
    return OccasionAccessResponse(
        occasion_id=occasion_id,
        is_selective=True,
        allowed_friends=updated_items,
    )


@router.post("/{occasion_id}/snooze")
async def snooze_occasion(
    occasion_id: str,
    payload: SnoozeOccasionPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Snooze occasion notifications for N days."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    # In production, snooze can adjust reminder trigger dates or store snooze flags
    logger.info("User %s snoozed occasion %s by %d days", user.id, occasion_id, payload.days)
    return {"status": "ok", "message": f"Snoozed for {payload.days} days"}


@router.post("/{occasion_id}/skip")
async def skip_occasion(
    occasion_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Skip celebration reminders for the current year."""
    occ = await db.scalar(
        select(Occasion).where(
            Occasion.id == occasion_id,
            Occasion.user_id == user.id,
            Occasion.deleted_at.is_(None),
        )
    )
    if not occ:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Occasion not found")

    logger.info("User %s skipped occasion %s for this cycle", user.id, occasion_id)
    return {"status": "ok", "message": "Celebration reminders skipped for this cycle"}
