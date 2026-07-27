"""Corporate gifting lead capture endpoint."""

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models import CorporateLead, HoneypotHit
from app.utils.email import send_email
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter

logger = logging.getLogger("wishkr.corporate_leads")

router = APIRouter(prefix="/corporate", tags=["corporate"])

_PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
_DUPLICATE_WINDOW = timedelta(days=14)
_corporate_rate_limiter = RateLimiter(max_requests=4, window_seconds=600, name="corporate")
_LOGO_UPLOAD_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_LOGO_BYTES = 5 * 1024 * 1024

CONSENT_TEXT = "Wishkr may contact me about corporate gifting."
CONSENT_TEXT_HASH = hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()[:32]


class CorporateLeadIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=160)
    contact_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=30)
    role: str | None = Field(default=None, max_length=80)
    recipient_count: int | None = Field(default=None, ge=1, le=100000)
    occasion: str | None = Field(default=None, max_length=80)
    budget_per_recipient: str | None = Field(default=None, max_length=80)
    timeline: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    logo_url: str | None = Field(default=None, max_length=500)
    delivery_type: str | None = Field(default=None, max_length=20)
    needed_by: date | None = None
    notes: str | None = Field(default=None, max_length=3000)
    consent_given: bool
    source: str | None = Field(default=None, max_length=40)
    website_url: str | None = Field(default=None, max_length=255)

    @field_validator("delivery_type")
    @classmethod
    def _delivery_type_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip().lower()
        if value not in ("single", "multi-address"):
            raise ValueError("delivery_type must be 'single' or 'multi-address'")
        return value

    @field_validator("phone")
    @classmethod
    def _phone_shape(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        value = v.strip()
        if not _PHONE_RE.match(value):
            raise ValueError("invalid phone format")
        return value

    @field_validator(
        "company_name",
        "contact_name",
        "role",
        "occasion",
        "budget_per_recipient",
        "timeline",
        "city",
        "notes",
        "source",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @model_validator(mode="after")
    def _requires_consent(self) -> "CorporateLeadIn":
        if not self.consent_given:
            raise ValueError("consent is required to submit")
        return self


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/leads/logo")
async def upload_corporate_logo(request: Request, file: UploadFile = File(...)) -> dict:
    """Buyer-facing logo upload for the corporate enquiry form, before a lead exists."""
    ip = _client_ip(request)
    if not await _corporate_rate_limiter.allow(ip):
        raise HTTPException(status_code=429, detail="Too many uploads. Try again later.")

    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise HTTPException(status_code=503, detail="uploads are not configured")

    content_type = (file.content_type or "").lower()
    suffix = _LOGO_UPLOAD_TYPES.get(content_type)
    if suffix is None:
        raise HTTPException(status_code=422, detail="only JPEG, PNG, or WEBP logos are accepted")

    body = await file.read()
    if len(body) > _MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="logo must be 5 MB or smaller")

    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc

    # Ignore the client-supplied filename entirely — never trust it for the key.
    _ = PurePosixPath(file.filename or "")
    key = f"corporate-logos/{uuid4().hex}{suffix}"

    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )
    client.put_object(Bucket=settings.r2_bucket, Key=key, Body=body, ContentType=content_type)

    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    return {"url": public_url}


@router.post("/leads", status_code=201)
async def submit_corporate_lead(
    payload: CorporateLeadIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:500]

    if payload.website_url and payload.website_url.strip():
        logger.warning(
            "corporate_lead_honeypot_tripped ip=%s email=%s ua=%s",
            ip,
            payload.email,
            ua[:80],
        )
        try:
            db.add(
                HoneypotHit(
                    id=generate_id("hp"),
                    endpoint="/corporate/leads",
                    ip=ip[:45],
                    user_agent=ua,
                    email=str(payload.email)[:255],
                    brand_name=payload.company_name[:120],
                    notes=(payload.website_url or "")[:1000],
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("honeypot_hit persist failed (corporate)")
        return {"success": True, "id": generate_id("clead")}

    if not await _corporate_rate_limiter.allow(ip):
        logger.warning("corporate_lead_rate_limited ip=%s email=%s", ip, payload.email)
        raise HTTPException(status_code=429, detail="Too many submissions. Try again later.")

    email_norm = str(payload.email).lower()
    company_norm = payload.company_name.strip()
    cutoff = datetime.now(timezone.utc) - _DUPLICATE_WINDOW
    dupe = (
        await db.execute(
            select(CorporateLead).where(
                CorporateLead.email == email_norm,
                CorporateLead.company_name == company_norm,
                CorporateLead.created_at >= cutoff,
            )
        )
    ).scalar_one_or_none()
    if dupe:
        logger.info("corporate_lead_duplicate id=%s email=%s ip=%s", dupe.id, email_norm, ip)
        return {"success": True, "id": dupe.id, "duplicate": True}

    lead = CorporateLead(
        id=generate_id("clead"),
        company_name=company_norm,
        contact_name=payload.contact_name.strip(),
        email=email_norm,
        phone=payload.phone,
        role=payload.role,
        recipient_count=payload.recipient_count,
        occasion=payload.occasion,
        budget_per_recipient=payload.budget_per_recipient,
        timeline=payload.timeline,
        city=payload.city,
        logo_url=payload.logo_url,
        delivery_type=payload.delivery_type,
        needed_by=payload.needed_by,
        notes=payload.notes,
        consent_given=payload.consent_given,
        source=payload.source or "website",
        status="new",
        ip=ip[:45],
        user_agent=ua,
    )
    db.add(lead)
    await db.commit()
    logger.info("corporate_lead_created id=%s email=%s", lead.id, lead.email)

    asyncio.create_task(_notify_hello(lead))
    return {"success": True, "id": lead.id}


async def _notify_hello(lead: CorporateLead) -> None:
    subject = f"Corporate gifting lead: {lead.company_name}"
    text = "\n".join(
        [
            "New corporate gifting lead",
            "",
            f"Company: {lead.company_name}",
            f"Contact: {lead.contact_name}",
            f"Email: {lead.email}",
            f"Phone: {lead.phone or '-'}",
            f"Role: {lead.role or '-'}",
            f"Recipients: {lead.recipient_count or '-'}",
            f"Occasion: {lead.occasion or '-'}",
            f"Budget per recipient: {lead.budget_per_recipient or '-'}",
            f"Timeline: {lead.timeline or '-'}",
            f"City: {lead.city or '-'}",
            f"Needed by: {lead.needed_by.isoformat() if lead.needed_by else '-'}",
            f"Delivery type: {lead.delivery_type or '-'}",
            f"Logo: {lead.logo_url or '-'}",
            f"Source: {lead.source or '-'}",
            f"Consent hash: {CONSENT_TEXT_HASH}",
            "",
            "Notes:",
            lead.notes or "-",
        ]
    )
    try:
        await send_email(
            to_email=settings.corporate_lead_email_to,
            subject=subject,
            text=text,
            reply_to=lead.email,
        )
    except Exception:
        logger.exception("corporate_lead_email_failed id=%s", lead.id)
