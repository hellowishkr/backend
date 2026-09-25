"""Mobile Preferences API routes for wishkr_app.

Endpoints:
  GET  /api/v1/preferences          — Get current user's gifting preferences
  PUT  /api/v1/preferences          — Update current user's gifting preferences
  POST /api/v1/preferences/request  — Trigger preference collection request to contact
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mobile_auth import get_current_mobile_user
from app.db.session import get_db
from app.models.recipient_preference import PreferenceRequest, RecipientPreference
from app.models.user import User
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.mobile_preferences")

router = APIRouter(prefix="/v1/preferences", tags=["mobile_preferences"])


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


# ── Schemas ───────────────────────────────────────────────────────────────────


class PreferencesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone: str
    preferred_categories: list[str] = Field(default_factory=list)
    preferred_budget: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    notes: str | None = None
    updated_at: str | None = None


class UpdatePreferencesPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categories: list[str] = Field(default_factory=list, description="Vibe/category tags")
    preferred_budget: str | None = Field(None, description="e.g. ₹1,000 – ₹3,000")
    budget_min: int | None = Field(None, ge=0)
    budget_max: int | None = Field(None, ge=0)
    notes: str | None = Field(None, max_length=500, description="Specific notes / gift hints")


class PreferenceRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contact_phone: str = Field(..., description="Friend's phone number")
    occasion_id: str | None = Field(None, description="Associated occasion ID if any")
    anonymous: bool = Field(True, description="Whether request is surprise/anonymous")


class PreferenceRequestResponse(BaseModel):
    request_id: str
    recipient_phone: str
    status: str
    created_at: str
    message: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=PreferencesResponse)
async def get_my_preferences(
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferencesResponse:
    """Retrieve the current user's gifting preferences."""
    clean_p = _clean_phone(user.phone)
    if not clean_p:
        return PreferencesResponse(phone=user.phone or "")

    pref = await db.scalar(
        select(RecipientPreference).where(RecipientPreference.recipient_phone == clean_p)
    )

    if not pref:
        return PreferencesResponse(phone=clean_p)

    cats = [c.strip() for c in (pref.preferred_categories or "").split(",") if c.strip()]
    return PreferencesResponse(
        phone=clean_p,
        preferred_categories=cats,
        budget_min=pref.preferred_budget_min,
        budget_max=pref.preferred_budget_max,
        notes=pref.notes,
        updated_at=pref.updated_at.isoformat() if pref.updated_at else None,
    )


@router.put("", response_model=PreferencesResponse)
async def update_my_preferences(
    payload: UpdatePreferencesPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferencesResponse:
    """Update or create the current user's gifting preferences."""
    clean_p = _clean_phone(user.phone)
    if not clean_p:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User does not have a verified phone number.",
        )

    pref = await db.scalar(
        select(RecipientPreference).where(RecipientPreference.recipient_phone == clean_p)
    )

    categories_csv = ",".join(c.strip() for c in payload.categories if c.strip())

    if not pref:
        pref = RecipientPreference(
            id=generate_id("prf"),
            recipient_phone=clean_p,
            preferred_categories=categories_csv,
            preferred_budget_min=payload.budget_min,
            preferred_budget_max=payload.budget_max,
            notes=payload.notes.strip() if payload.notes else None,
            source="manual",
        )
        db.add(pref)
    else:
        pref.preferred_categories = categories_csv
        pref.preferred_budget_min = payload.budget_min
        pref.preferred_budget_max = payload.budget_max
        pref.notes = payload.notes.strip() if payload.notes else None
        pref.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(pref)

    logger.info("User %s updated preferences (%d vibes)", user.id, len(payload.categories))
    return PreferencesResponse(
        phone=clean_p,
        preferred_categories=[c.strip() for c in categories_csv.split(",") if c.strip()],
        budget_min=pref.preferred_budget_min,
        budget_max=pref.preferred_budget_max,
        notes=pref.notes,
        updated_at=pref.updated_at.isoformat() if pref.updated_at else None,
    )


@router.post(
    "/request", response_model=PreferenceRequestResponse, status_code=status.HTTP_201_CREATED
)
async def request_friend_preferences(
    payload: PreferenceRequestPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PreferenceRequestResponse:
    """Trigger a preference request to a friend (via push if on app, or WhatsApp)."""
    clean_recip = _clean_phone(payload.contact_phone)
    if not clean_recip:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid recipient phone number.",
        )

    clean_requester = _clean_phone(user.phone) or user.phone

    # Deduplicate pending request for this pair
    existing = await db.scalar(
        select(PreferenceRequest).where(
            PreferenceRequest.recipient_phone == clean_recip,
            PreferenceRequest.occasion_id == payload.occasion_id,
            PreferenceRequest.status == "pending",
        )
    )

    if existing:
        return PreferenceRequestResponse(
            request_id=existing.id,
            recipient_phone=clean_recip,
            status=existing.status,
            created_at=existing.created_at.isoformat(),
            message="A request is already pending for this contact and occasion.",
        )

    req_id = generate_id("prq")
    req = PreferenceRequest(
        id=req_id,
        recipient_phone=clean_recip,
        occasion_id=payload.occasion_id,
        requester_phone=clean_requester,
        status="pending",
        anonymous=payload.anonymous,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    logger.info("Created preference request %s from %s to %s", req.id, clean_requester, clean_recip)
    return PreferenceRequestResponse(
        request_id=req.id,
        recipient_phone=clean_recip,
        status="pending",
        created_at=req.created_at.isoformat(),
        message="Preference request queued successfully.",
    )
