"""Lead-capture endpoint for the /vendor-app download gate.

Lightweight contact form shown right before a vendor downloads the desktop
installer. Separate from /partners/vendor-leads (the full partner application).
"""

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import HoneypotHit, VendorAppDownload
from app.utils.alerts import send_founder_alert
from app.utils.id_gen import generate_id
from app.utils.rate_limit import RateLimiter

logger = logging.getLogger("wishkr.vendor_app_downloads")

router = APIRouter(prefix="/vendor-app", tags=["vendor-app"])

_PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
_ALLOWED_PLATFORMS = {"windows", "macos", "linux"}

CONSENT_VERSION = "v1.2026-05"
CONSENT_TEXT = (
    "I agree to Wishkr contacting me about the Wishkr Vendor app and "
    "processing the information I've provided for that purpose."
)
CONSENT_TEXT_HASH = hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()[:32]

_DUPLICATE_WINDOW = timedelta(days=7)

# Slightly looser than partner intake: this is a download gate, not a curated funnel.
_rate_limiter = RateLimiter(max_requests=5, window_seconds=600, name="app_downloads")


class VendorAppDownloadIn(BaseModel):
    contact_name: str = Field(min_length=2, max_length=100)
    brand_name: str | None = Field(default=None, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=7, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    platform: str = Field(min_length=3, max_length=20)
    app_version: str | None = Field(default=None, max_length=20)
    consent_given: bool
    # Honeypot.
    website_url: str | None = Field(default=None, max_length=255)

    @field_validator("phone")
    @classmethod
    def _phone_shape(cls, v: str) -> str:
        if not _PHONE_RE.match(v.strip()):
            raise ValueError("invalid phone format")
        return v.strip()

    @field_validator("platform")
    @classmethod
    def _platform(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _ALLOWED_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(_ALLOWED_PLATFORMS)}")
        return v

    @field_validator("consent_given")
    @classmethod
    def _must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("consent is required to submit")
        return v


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# Domains that serve the download files (same Vercel app is aliased to both,
# and /downloads/* is excluded from the canonical-host redirect). Some networks
# block one TLD, so we hand back a URL on the SAME domain the request came from.
_DOWNLOAD_HOSTS = {
    "wishkr.com": "https://wishkr.com/downloads",
    "www.wishkr.com": "https://www.wishkr.com/downloads",
    "wishkr.in": "https://wishkr.in/downloads",
    "www.wishkr.in": "https://www.wishkr.in/downloads",
}
_DEFAULT_DOWNLOAD_BASE = "https://wishkr.com/downloads"


def _download_base(request: Request) -> str:
    """Pick the download base URL matching the domain the request came from.

    Falls back to the canonical .com base for any unrecognised origin, so a
    spoofed Origin/Referer can never redirect the download to a foreign host.
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = urlparse(origin).hostname
    if host and host in _DOWNLOAD_HOSTS:
        return _DOWNLOAD_HOSTS[host]
    return _DEFAULT_DOWNLOAD_BASE


def _download_url(platform: str, version: str | None, base: str) -> str:
    """Resolve the public installer URL for a given platform.

    Files are produced by `npm run publish` in wishkr-vendor-app and copied to
    wishkr-web/public/downloads/. Filename convention is stable per platform
    so the website can hot-swap a new build without API changes.
    """
    if platform == "windows":
        return f"{base}/wishkr-vendor-setup.exe"
    if platform == "macos":
        return f"{base}/wishkr-vendor.dmg"
    if platform == "linux":
        return f"{base}/wishkr-vendor.AppImage"
    return f"{base}/wishkr-vendor-setup.exe"  # safe default


@router.post("/downloads", status_code=201)
async def submit_download_lead(
    payload: VendorAppDownloadIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:500]
    referrer = (request.headers.get("referer") or "")[:500] or None
    base = _download_base(request)

    # Honeypot.
    if payload.website_url and payload.website_url.strip():
        logger.warning(
            "vendor_app_download_honeypot_tripped ip=%s email=%s", ip, payload.email,
        )
        try:
            db.add(HoneypotHit(
                id=generate_id("hp"),
                endpoint="/vendor-app/downloads",
                ip=ip[:45],
                user_agent=ua,
                email=str(payload.email)[:255],
                brand_name=(payload.brand_name or "")[:120] or None,
                notes=(payload.website_url or "")[:1000],
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("honeypot_hit persist failed")
        return {
            "success": True,
            "id": generate_id("vdl"),
            "download_url": _download_url(payload.platform, payload.app_version, base),
        }

    if not await _rate_limiter.allow(ip):
        logger.warning("vendor_app_download_rate_limited ip=%s email=%s", ip, payload.email)
        raise HTTPException(status_code=429, detail="Too many submissions. Try again later.")

    email_norm = str(payload.email).lower()

    # Duplicate guard: same email within window → idempotent, still return URL.
    cutoff = datetime.now(timezone.utc) - _DUPLICATE_WINDOW
    dupe = (await db.execute(
        select(VendorAppDownload).where(
            VendorAppDownload.email == email_norm,
            VendorAppDownload.created_at >= cutoff,
        ).limit(1)
    )).scalar_one_or_none()
    if dupe is not None:
        logger.info("vendor_app_download_duplicate id=%s email=%s ip=%s", dupe.id, email_norm, ip)
        return {
            "success": True,
            "id": dupe.id,
            "duplicate": True,
            "download_url": _download_url(payload.platform, payload.app_version, base),
        }

    row = VendorAppDownload(
        id=generate_id("vdl"),
        contact_name=payload.contact_name.strip(),
        brand_name=payload.brand_name.strip() if payload.brand_name else None,
        email=email_norm,
        phone=payload.phone,
        city=payload.city.strip() if payload.city else None,
        platform=payload.platform,
        app_version=payload.app_version,
        consent_given=payload.consent_given,
        consent_version=CONSENT_VERSION,
        consent_text_hash=CONSENT_TEXT_HASH,
        user_agent=ua,
        referrer=referrer,
        status="new",
    )
    db.add(row)
    await db.commit()

    logger.info(
        "vendor_app_download_lead id=%s email=%s phone=%s platform=%s ver=%s ip=%s",
        row.id, email_norm, payload.phone, payload.platform, payload.app_version, ip,
    )

    asyncio.create_task(_notify_founder(row))

    return {
        "success": True,
        "id": row.id,
        "download_url": _download_url(payload.platform, payload.app_version, base),
    }


async def _notify_founder(row: VendorAppDownload) -> None:
    try:
        msg = (
            f"⬇️ Vendor app download: {row.contact_name}"
            f"{f' ({row.brand_name})' if row.brand_name else ''}\n"
            f"Contact: {row.email} / {row.phone}\n"
            f"City: {row.city or '—'} | Platform: {row.platform} | App: {row.app_version or '—'}\n"
            f"ID: {row.id}"
        )
        await send_founder_alert(msg)
    except Exception:
        logger.exception("vendor_app_download_alert_failed id=%s", row.id)
