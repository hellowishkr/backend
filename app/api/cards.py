"""Hubble SDK gift-card SSO token endpoint."""

import logging
import re
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.core.config import settings
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter

logger = logging.getLogger("wishkr.cards")

router = APIRouter(prefix="/cards", tags=["cards"])

_PHONE_RE = re.compile(r"^[0-9]{10}$")
_cards_rate_limiter = RateLimiter(max_requests=6, window_seconds=600, name="cards")


class CardsSsoTokenIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=30)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("phone")
    @classmethod
    def _clean_phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        cleaned = v.strip()
        if not _PHONE_RE.match(cleaned):
            raise ValueError("phone must be exactly 10 digits without country code")
        return cleaned

    @model_validator(mode="after")
    def _require_contact(self) -> "CardsSsoTokenIn":
        if self.email is None and not self.phone:
            raise ValueError("email or phone is required")
        return self


class CardsSsoTokenOut(BaseModel):
    token: str
    sdkBaseUrl: str
    clientId: str


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _cards_sdk_configured() -> bool:
    return all(
        [
            settings.hubble_sdk_enabled,
            settings.hubble_sdk_client_id.strip(),
            settings.hubble_sdk_app_secret.strip(),
            settings.hubble_sdk_private_key.strip() or settings.hubble_sdk_mock_token.strip(),
        ]
    )


@router.post("/sso-token", response_model=CardsSsoTokenOut)
async def create_cards_sso_token(payload: CardsSsoTokenIn, request: Request) -> CardsSsoTokenOut:
    ip = _client_ip(request)
    if not await _cards_rate_limiter.allow(ip):
        logger.warning("cards_sso_rate_limited ip=%s", ip)
        raise HTTPException(status_code=429, detail="Too many submissions. Try again later.")

    if not _cards_sdk_configured():
        logger.warning("cards_sso_not_configured")
        raise HTTPException(status_code=503, detail="Gift card store is not configured")

    private_key = settings.hubble_sdk_private_key.strip().replace("\\n", "\n")
    if private_key:
        issued_at = int(datetime.now(timezone.utc).timestamp())
        claims: dict[str, object] = {
            "sub": generate_id("hbl"),
            "iss": settings.hubble_sdk_client_id,
            "iat": issued_at,
            "exp": issued_at + 60,
            "name": payload.name,
        }
        if payload.email is not None:
            claims["email"] = str(payload.email).lower()
        if payload.phone is not None:
            claims["phoneNumber"] = payload.phone

        try:
            token = jwt.encode(claims, private_key, algorithm="RS256")
        except Exception as exc:
            logger.exception("cards_sso_sign_failed error=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Gift card store is not configured") from exc
    else:
        token = settings.hubble_sdk_mock_token.strip()

    logger.info(
        "cards_sso_token_minted ip=%s has_email=%s has_phone=%s",
        ip,
        payload.email is not None,
        payload.phone is not None,
    )
    return CardsSsoTokenOut(
        token=token,
        sdkBaseUrl=settings.hubble_sdk_effective_base_url,
        clientId=settings.hubble_sdk_client_id,
    )
