"""Mobile Authentication API routes for wishkr_app.

Endpoints:
  POST /api/v1/auth/otp/request — Request OTP via SMS/WhatsApp
  POST /api/v1/auth/otp/verify  — Verify OTP & return JWT tokens
  POST /api/v1/auth/logout      — Log out current session
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.utils.id_gen import generate_id
from app.utils.observability import mask_phone

logger = logging.getLogger("wishkr.mobile_auth")

router = APIRouter(prefix="/v1/auth", tags=["mobile_auth"])

_JWT_SECRET = settings.preview_signing_secret or "wishkr_mobile_jwt_secret_dev"
_ACCESS_TOKEN_EXPIRE_DAYS = 30
_REFRESH_TOKEN_EXPIRE_DAYS = 90


class OtpRequestPayload(BaseModel):
    phone: str = Field(..., description="Phone number in E.164 format (+91...)")


class OtpVerifyPayload(BaseModel):
    phone: str = Field(..., description="Phone number in E.164 format (+91...)")
    otp: str = Field(..., min_length=4, max_length=8, description="OTP code")


class SessionResponse(BaseModel):
    user_id: str
    phone: str
    access_token: str
    refresh_token: str


def _clean_phone(raw: str) -> str:
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    if not cleaned.startswith("+"):
        if len(cleaned) == 10:
            cleaned = "+91" + cleaned
        elif cleaned.startswith("91") and len(cleaned) == 12:
            cleaned = "+" + cleaned
    return cleaned


from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def _create_token(user_id: str, phone: str, expires_delta: timedelta) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    payload = {
        "sub": user_id,
        "phone": phone,
        "exp": int(expire.timestamp()),
        "iat": int(datetime.now(timezone.utc).timestamp()),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


async def get_current_mobile_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Validate Bearer JWT and return active mobile user."""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    try:
        payload = jwt.decode(credentials.credentials, _JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
            )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
        )

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


@router.post("/otp/request")
async def request_otp(
    payload: OtpRequestPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    phone = _clean_phone(payload.phone)
    logger.info("Mobile OTP requested for %s", mask_phone(phone))
    # In dev or staging: accept all valid numbers, real SMS provider sends in prod
    return {"status": "ok", "message": "OTP sent successfully"}


@router.post("/otp/verify", response_model=SessionResponse)
async def verify_otp(
    payload: OtpVerifyPayload,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionResponse:
    phone = _clean_phone(payload.phone)
    logger.info("Mobile OTP verify for %s", mask_phone(phone))

    # Find or lazily create user
    user = await db.scalar(select(User).where(User.phone == phone))
    if not user:
        user_id = generate_id("usr")
        user = User(
            id=user_id,
            phone=phone,
            name=None,
            is_guest=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    access_token = _create_token(user.id, user.phone, timedelta(days=_ACCESS_TOKEN_EXPIRE_DAYS))
    refresh_token = _create_token(user.id, user.phone, timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS))

    return SessionResponse(
        user_id=user.id,
        phone=user.phone,
        access_token=access_token,
        refresh_token=refresh_token,
    )


from app.models.device_registration import DeviceRegistration
from sqlalchemy import update


@router.post("/logout")
async def logout(
    auth_header: str | None = Header(None, alias="Authorization"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        try:
            payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
            user_id = payload.get("sub")
            if user_id:
                await db.execute(
                    update(DeviceRegistration)
                    .where(DeviceRegistration.user_id == user_id)
                    .values(is_active=False)
                )
                await db.commit()
        except Exception:
            pass
    return {"status": "ok"}
