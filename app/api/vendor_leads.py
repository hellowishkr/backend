"""Public endpoint for vendor/partner applications from the website.

Writes to `vendor_leads` — a raw inbox decoupled from the curated `vendors`
catalogue. Admin triages from there (pre-launch volume is low enough for SQL).
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.categories import OTHER_CATEGORY_SENTINEL, PARTNER_CATEGORIES
from app.db.session import get_db
from app.models import HoneypotHit, VendorLead
from app.utils.alerts import send_founder_alert
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter

logger = logging.getLogger("wishkr.partners")

router = APIRouter(prefix="/partners", tags=["partners"])

_PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
_URL_RE = re.compile(r"^https?://", re.I)

# DPDP Act 2023: persist enough to prove *what* the user consented to.
# Bump the version string whenever the consent copy on the form changes.
CONSENT_VERSION = "v1.2026-04"
CONSENT_TEXT = (
    "I agree to Wishkr contacting me about partnership opportunities and "
    "processing the information I've provided for that purpose."
)
CONSENT_TEXT_HASH = hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()[:32]

# Duplicate-submission window: same brand+email within this period is rejected.
_DUPLICATE_WINDOW = timedelta(days=30)

# Rate limiter: 3 submissions per IP per 10 min. Shared, pruned instance.
_partner_rate_limiter = RateLimiter(max_requests=3, window_seconds=600, name="partners")


class VendorLeadIn(BaseModel):
    brand_name: str = Field(min_length=2, max_length=120)
    contact_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=255)
    instagram: str | None = Field(default=None, max_length=100)
    categories: list[str] = Field(min_length=1, max_length=20)
    other_category: str | None = Field(default=None, max_length=80)
    cities_raw: str = Field(min_length=2, max_length=500)
    price_range: str | None = Field(default=None, max_length=60)
    monthly_capacity: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    consent_given: bool
    source: str | None = Field(default=None, max_length=40)
    # ISO 3166-1 alpha-2 country code (or "EU"/"OTHER") — drives currency/tax
    # pre-fill in the vendor app on first launch and segments lead reporting.
    country: str | None = Field(default=None, max_length=8)
    # Honeypot: rendered hidden on the form. Real users never fill it; bots do.
    website_url: str | None = Field(default=None, max_length=255)

    @field_validator("phone")
    @classmethod
    def _phone_shape(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("invalid phone format")
        return v.strip()

    @field_validator("website")
    @classmethod
    def _website_shape(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not _URL_RE.match(v.strip()):
            raise ValueError("website must start with http:// or https://")
        return v.strip()

    @field_validator("categories")
    @classmethod
    def _clean_categories(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip().lower() for c in v if c and c.strip()]
        if not cleaned:
            raise ValueError("at least one category is required")
        # Dedupe while preserving order, cap to 20.
        deduped = list(dict.fromkeys(cleaned))
        allowed = PARTNER_CATEGORIES | {OTHER_CATEGORY_SENTINEL}
        unknown = [c for c in deduped if c not in allowed]
        if unknown:
            raise ValueError(f"unknown category: {unknown[0]}")
        return deduped[:20]

    @model_validator(mode="after")
    def _resolve_other(self) -> "VendorLeadIn":
        """Replace the 'other' sentinel with the user-typed free-text value."""
        if OTHER_CATEGORY_SENTINEL in self.categories:
            text = (self.other_category or "").strip().lower()
            if len(text) < 2:
                raise ValueError("please specify your category when selecting Other")
            rest = [c for c in self.categories if c != OTHER_CATEGORY_SENTINEL]
            if text not in rest and text not in PARTNER_CATEGORIES:
                rest.append(text[:60])
            self.__dict__["categories"] = rest[:20]
        self.__dict__["other_category"] = None  # not persisted separately
        return self

    @field_validator("consent_given")
    @classmethod
    def _must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("consent is required to submit")
        return v


def _client_ip(request: Request) -> str:
    """Real client IP when behind a trusted reverse proxy.

    Relies on uvicorn being started with --proxy-headers (or equivalent),
    which rewrites request.client.host from the first X-Forwarded-For hop.
    We also read the header directly as a belt-and-braces fallback when
    uvicorn's proxy trust list doesn't include the proxy's address.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@router.post("/vendor-leads", status_code=201)
async def submit_vendor_lead(
    payload: VendorLeadIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    ip = _client_ip(request)

    # Honeypot: silently accept (avoid telling bots they've been caught) but
    # don't persist a vendor_lead row or notify the founder. We DO persist a
    # row to ``honeypot_hits`` so a future abuse-analytics pass can correlate
    # campaigns by IP / UA / domain. Persist failures must not surface to the
    # bot — fall through to the normal success-shaped response either way.
    if payload.website_url and payload.website_url.strip():
        ua = (request.headers.get("user-agent") or "")[:500]
        logger.warning(
            "partner_lead_honeypot_tripped ip=%s email=%s ua=%s",
            ip, payload.email, ua[:80],
        )
        try:
            db.add(HoneypotHit(
                id=generate_id("hp"),
                endpoint="/partners/vendor-leads",
                ip=ip[:45],
                user_agent=ua,
                email=str(payload.email)[:255],
                brand_name=payload.brand_name[:120] if payload.brand_name else None,
                notes=(payload.website_url or "")[:1000],
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("honeypot_hit persist failed")
        return {"success": True, "id": generate_id("vlead")}

    if not await _partner_rate_limiter.allow(ip):
        logger.warning("partner_lead_rate_limited ip=%s email=%s", ip, payload.email)
        raise HTTPException(status_code=429, detail="Too many submissions. Try again later.")

    email_norm = str(payload.email).lower()
    brand_norm = payload.brand_name.strip()

    # Duplicate guard: same brand+email within window → idempotent response.
    cutoff = datetime.now(timezone.utc) - _DUPLICATE_WINDOW
    dupe = (await db.execute(
        select(VendorLead).where(
            VendorLead.email == email_norm,
            VendorLead.brand_name == brand_norm,
            VendorLead.created_at >= cutoff,
        ).limit(1)
    )).scalar_one_or_none()
    if dupe is not None:
        logger.info(
            "partner_lead_duplicate id=%s email=%s brand=%s ip=%s",
            dupe.id, email_norm, brand_norm, ip,
        )
        return {"success": True, "id": dupe.id, "duplicate": True}

    lead = VendorLead(
        id=generate_id("vlead"),
        brand_name=brand_norm,
        contact_name=payload.contact_name.strip(),
        email=email_norm,
        phone=payload.phone,
        website=payload.website,
        instagram=payload.instagram.strip() if payload.instagram else None,
        categories=payload.categories,
        cities_raw=payload.cities_raw.strip(),
        country=payload.country.strip().upper()[:8] if payload.country else None,
        price_range=payload.price_range.strip() if payload.price_range else None,
        monthly_capacity=payload.monthly_capacity.strip() if payload.monthly_capacity else None,
        notes=payload.notes.strip() if payload.notes else None,
        consent_given=payload.consent_given,
        consent_version=CONSENT_VERSION,
        consent_text_hash=CONSENT_TEXT_HASH,
        source=(payload.source or "website").strip()[:40],
        status="new",
    )
    db.add(lead)
    await db.commit()

    logger.info(
        "partner_lead_created id=%s brand=%s email=%s cities=%s categories=%s ip=%s",
        lead.id, brand_norm, email_norm, lead.cities_raw[:80], payload.categories, ip,
    )

    # Fire-and-forget founder notification. Failure must never block the 201.
    asyncio.create_task(_notify_founder(lead))

    return {"success": True, "id": lead.id}


async def _notify_founder(lead: VendorLead) -> None:
    try:
        msg = (
            f"🆕 Partner lead: {lead.brand_name}\n"
            f"Contact: {lead.contact_name} <{lead.email}>"
            f"{f' / {lead.phone}' if lead.phone else ''}\n"
            f"Cities: {lead.cities_raw[:120]}\n"
            f"Categories: {', '.join(lead.categories[:5])}\n"
            f"ID: {lead.id}"
        )
        await send_founder_alert(msg)
    except Exception:  # pragma: no cover — alert must not break request
        logger.exception("partner_lead_alert_failed id=%s", lead.id)
