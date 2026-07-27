"""Public endpoint for user-submitted feedback from the website and vendor app.

Lightweight inbox: no consent gate (no required PII), honeypot + rate-limit
guarded, founder alert on each new submission. Admin triages by status.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import Feedback, HoneypotHit
from app.utils.alerts import send_founder_alert
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter

logger = logging.getLogger("wishkr.feedback")

router = APIRouter(prefix="/feedback", tags=["feedback"])

_ALLOWED_CATEGORIES = {
    "general feedback",
    "bug report",
    "feature request",
    "vendor app feedback",
    "website / ux",
    "pricing or billing",
    "other",
}

_ALLOWED_SOURCES = {"website", "vendor_app", "android", "ios"}

# 5 submissions per IP per 10 min — slightly more lenient than vendor leads
# since feedback often comes in bursts (user lists 3 things in a row).
_feedback_rate_limiter = RateLimiter(max_requests=5, window_seconds=600, name="feedback")


class FeedbackIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    category: str = Field(min_length=2, max_length=60)
    rating: int | None = Field(default=None, ge=1, le=5)
    message: str = Field(min_length=5, max_length=4000)
    source: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=40)
    # Honeypot: hidden on the form. Real users never fill it; bots do.
    website_url: str | None = Field(default=None, max_length=255)

    @field_validator("category")
    @classmethod
    def _normalize_category(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _ALLOWED_CATEGORIES:
            # Accept unknown categories rather than rejecting — easier to evolve
            # the form copy without coordinating a backend deploy. Cap length.
            return v.strip()[:60]
        return normalized

    @field_validator("source")
    @classmethod
    def _normalize_source(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.strip().lower()
        if not normalized:
            return None
        # Same philosophy as category — don't gate evolution on the backend.
        return normalized[:40]


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@router.post("", status_code=201)
async def submit_feedback(
    payload: FeedbackIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:500]

    # Honeypot: silently 201 (don't tip bots off), persist the hit for analytics.
    if payload.website_url and payload.website_url.strip():
        logger.warning(
            "feedback_honeypot_tripped ip=%s email=%s ua=%s",
            ip, payload.email, ua[:80],
        )
        try:
            db.add(HoneypotHit(
                id=generate_id("hp"),
                endpoint="/feedback",
                ip=ip[:45],
                user_agent=ua,
                email=str(payload.email)[:255] if payload.email else None,
                brand_name=None,
                notes=(payload.website_url or "")[:1000],
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("honeypot_hit persist failed (feedback)")
        return {"success": True, "id": generate_id("fb")}

    if not await _feedback_rate_limiter.allow(ip):
        logger.warning("feedback_rate_limited ip=%s email=%s", ip, payload.email)
        raise HTTPException(status_code=429, detail="Too many submissions. Try again later.")

    fb = Feedback(
        id=generate_id("fb"),
        name=payload.name.strip() if payload.name else None,
        email=str(payload.email).lower() if payload.email else None,
        category=payload.category,
        rating=payload.rating,
        message=payload.message.strip(),
        source=payload.source or "website",
        app_version=payload.app_version,
        user_agent=ua,
        ip=ip[:45],
        status="new",
    )
    db.add(fb)
    await db.commit()

    logger.info(
        "feedback_created id=%s category=%s source=%s email=%s ip=%s",
        fb.id, fb.category, fb.source, fb.email, ip,
    )

    asyncio.create_task(_notify_founder(fb))
    return {"success": True, "id": fb.id}


async def _notify_founder(fb: Feedback) -> None:
    try:
        snippet = fb.message[:280] + ("…" if len(fb.message) > 280 else "")
        rating = f" ★{fb.rating}/5" if fb.rating else ""
        contact = f"{fb.name or 'Anonymous'}"
        if fb.email:
            contact += f" <{fb.email}>"
        msg = (
            f"📝 Feedback ({fb.category}{rating}) — {fb.source}\n"
            f"From: {contact}\n"
            f"{snippet}\n"
            f"ID: {fb.id}"
        )
        await send_founder_alert(msg)
    except Exception:  # pragma: no cover — alert must not break request
        logger.exception("feedback_alert_failed id=%s", fb.id)
