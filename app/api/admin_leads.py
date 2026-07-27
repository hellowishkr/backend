"""Admin leads — manage vendor + corporate lead pipelines (Phase 3).

Lead intake already exists (POST /api/vendor-leads, /api/leads). This router adds
the manage layer: list, update status/notes, and convert a vendor lead into a
Vendor record (kicking off onboarding). Guarded by the admin token + role seam.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import _slugify_category, require_role
from app.db.session import get_db
from app.models.city import City
from app.models.corporate_lead import CorporateLead
from app.models.lead_activity import LeadActivity
from app.models.vendor import Vendor, VendorCategoryLink, VendorCity
from app.models.vendor_category import VendorCategory
from app.models.vendor_lead import VendorLead
from app.utils.id_gen import generate_id

router = APIRouter(prefix="/admin/leads", tags=["admin-leads"])

# Pipeline stages (stored in the free-text `status` column — no migration needed).
VENDOR_STAGES = ["new", "contacted", "interested", "pilot", "live", "rejected", "junk"]
CORPORATE_STAGES = ["new", "qualified", "quoted", "won", "lost", "junk"]
ACTIVITY_TYPES = ["note", "call", "status_change", "follow_up"]

_LEAD_MODELS = {"vendor": VendorLead, "corporate": CorporateLead}


def serialize_vendor_lead(lead: VendorLead) -> dict:
    return {
        "id": lead.id,
        "kind": "vendor",
        "brand_name": lead.brand_name,
        "contact_name": lead.contact_name,
        "email": lead.email,
        "phone": lead.phone,
        "website": lead.website,
        "instagram": lead.instagram,
        "categories": lead.categories or [],
        "cities_raw": lead.cities_raw,
        "country": lead.country,
        "price_range": lead.price_range,
        "monthly_capacity": lead.monthly_capacity,
        "notes": lead.notes,
        "source": lead.source,
        "status": lead.status,
        "next_follow_up_at": lead.next_follow_up_at.isoformat() if lead.next_follow_up_at else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "deleted_at": lead.deleted_at.isoformat() if lead.deleted_at else None,
    }


def serialize_corporate_lead(lead: CorporateLead) -> dict:
    return {
        "id": lead.id,
        "kind": "corporate",
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "email": lead.email,
        "phone": lead.phone,
        "role": lead.role,
        "recipient_count": lead.recipient_count,
        "occasion": lead.occasion,
        "budget_per_recipient": lead.budget_per_recipient,
        "timeline": lead.timeline,
        "city": lead.city,
        "logo_url": lead.logo_url,
        "delivery_type": lead.delivery_type,
        "needed_by": lead.needed_by.isoformat() if lead.needed_by else None,
        "notes": lead.notes,
        "source": lead.source,
        "status": lead.status,
        "next_follow_up_at": lead.next_follow_up_at.isoformat() if lead.next_follow_up_at else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "deleted_at": lead.deleted_at.isoformat() if lead.deleted_at else None,
    }


def serialize_activity(activity: LeadActivity) -> dict:
    return {
        "id": activity.id,
        "lead_kind": activity.lead_kind,
        "lead_id": activity.lead_id,
        "type": activity.type,
        "body": activity.body,
        "next_follow_up_at": activity.next_follow_up_at.isoformat() if activity.next_follow_up_at else None,
        "created_by": activity.created_by,
        "created_at": activity.created_at.isoformat() if activity.created_at else None,
    }


@router.get("/vendor", dependencies=[Depends(require_role("owner", "ops", "vendor_success"))])
async def list_vendor_leads(db: AsyncSession = Depends(get_db)) -> dict:
    leads = (
        await db.execute(
            select(VendorLead)
            .where(VendorLead.deleted_at.is_(None))
            .order_by(VendorLead.created_at.desc())
        )
    ).scalars().all()
    return {"stages": VENDOR_STAGES, "leads": [serialize_vendor_lead(l) for l in leads]}


@router.get("/corporate", dependencies=[Depends(require_role("owner", "ops"))])
async def list_corporate_leads(db: AsyncSession = Depends(get_db)) -> dict:
    leads = (
        await db.execute(
            select(CorporateLead)
            .where(CorporateLead.deleted_at.is_(None))
            .order_by(CorporateLead.created_at.desc())
        )
    ).scalars().all()
    return {"stages": CORPORATE_STAGES, "leads": [serialize_corporate_lead(l) for l in leads]}


class VendorLeadCreatePayload(BaseModel):
    """Admin-entered vendor lead (e.g. sourced offline, via call, or referral).

    Skips the public-form rules (category whitelist, phone regex, duplicate
    window, consent capture) — an admin adding a lead they already vetted
    doesn't need to be rate-limited or blocked by those guards.
    """

    brand_name: str = Field(min_length=2, max_length=120)
    contact_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=255)
    instagram: str | None = Field(default=None, max_length=100)
    categories: list[str] = Field(default_factory=list)
    cities_raw: str = Field(default="", max_length=500)
    country: str | None = Field(default=None, max_length=8)
    price_range: str | None = Field(default=None, max_length=60)
    monthly_capacity: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)


@router.post(
    "/vendor",
    status_code=201,
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def create_vendor_lead(
    payload: VendorLeadCreatePayload, db: AsyncSession = Depends(get_db)
) -> dict:
    lead = VendorLead(
        id=generate_id("vlead"),
        brand_name=payload.brand_name.strip(),
        contact_name=payload.contact_name.strip(),
        email=payload.email.strip().lower(),
        phone=payload.phone,
        website=payload.website,
        instagram=payload.instagram.strip() if payload.instagram else None,
        categories=payload.categories,
        cities_raw=payload.cities_raw.strip(),
        country=payload.country.strip().upper()[:8] if payload.country else None,
        price_range=payload.price_range,
        monthly_capacity=payload.monthly_capacity,
        notes=payload.notes,
        consent_given=True,
        source="admin_manual",
        status="new",
    )
    db.add(lead)
    await db.commit()
    return {"lead": serialize_vendor_lead(lead)}


class LeadUpdatePayload(BaseModel):
    """Partial update — only fields explicitly sent are changed."""

    status: str | None = None
    notes: str | None = None
    # Vendor-specific intake fields (ignored for corporate leads).
    brand_name: str | None = Field(default=None, max_length=120)
    contact_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=255)
    instagram: str | None = Field(default=None, max_length=100)
    categories: list[str] | None = None
    cities_raw: str | None = Field(default=None, max_length=500)
    country: str | None = Field(default=None, max_length=8)
    price_range: str | None = Field(default=None, max_length=60)
    monthly_capacity: str | None = Field(default=None, max_length=60)
    # Corporate-specific intake fields (ignored for vendor leads).
    company_name: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, max_length=80)
    recipient_count: int | None = None
    occasion: str | None = Field(default=None, max_length=80)
    budget_per_recipient: str | None = Field(default=None, max_length=80)
    timeline: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)


@router.patch(
    "/vendor/{lead_id}",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def update_vendor_lead(
    lead_id: str, payload: LeadUpdatePayload, db: AsyncSession = Depends(get_db)
) -> dict:
    lead = await db.get(VendorLead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise HTTPException(status_code=404, detail="lead not found")
    if payload.status is not None:
        if payload.status not in VENDOR_STAGES:
            raise HTTPException(status_code=400, detail="invalid status")
        lead.status = payload.status
    if payload.notes is not None:
        lead.notes = payload.notes
    if payload.brand_name is not None:
        lead.brand_name = payload.brand_name.strip()
    if payload.contact_name is not None:
        lead.contact_name = payload.contact_name.strip()
    if payload.email is not None:
        lead.email = payload.email.strip().lower()
    if payload.phone is not None:
        lead.phone = payload.phone.strip() or None
    if payload.website is not None:
        lead.website = payload.website.strip() or None
    if payload.instagram is not None:
        lead.instagram = payload.instagram.strip() or None
    if payload.categories is not None:
        lead.categories = payload.categories
    if payload.cities_raw is not None:
        lead.cities_raw = payload.cities_raw.strip()
    if payload.country is not None:
        lead.country = payload.country.strip().upper()[:8] or None
    if payload.price_range is not None:
        lead.price_range = payload.price_range.strip() or None
    if payload.monthly_capacity is not None:
        lead.monthly_capacity = payload.monthly_capacity.strip() or None
    await db.commit()
    return {"lead": serialize_vendor_lead(lead)}


@router.patch("/corporate/{lead_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def update_corporate_lead(
    lead_id: str, payload: LeadUpdatePayload, db: AsyncSession = Depends(get_db)
) -> dict:
    lead = await db.get(CorporateLead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise HTTPException(status_code=404, detail="lead not found")
    if payload.status is not None:
        if payload.status not in CORPORATE_STAGES:
            raise HTTPException(status_code=400, detail="invalid status")
        lead.status = payload.status
    if payload.notes is not None:
        lead.notes = payload.notes
    if payload.company_name is not None:
        lead.company_name = payload.company_name.strip()
    if payload.contact_name is not None:
        lead.contact_name = payload.contact_name.strip()
    if payload.email is not None:
        lead.email = payload.email.strip().lower()
    if payload.phone is not None:
        lead.phone = payload.phone.strip() or None
    if payload.role is not None:
        lead.role = payload.role.strip() or None
    if payload.recipient_count is not None:
        lead.recipient_count = payload.recipient_count
    if payload.occasion is not None:
        lead.occasion = payload.occasion.strip() or None
    if payload.budget_per_recipient is not None:
        lead.budget_per_recipient = payload.budget_per_recipient.strip() or None
    if payload.timeline is not None:
        lead.timeline = payload.timeline.strip() or None
    if payload.city is not None:
        lead.city = payload.city.strip() or None
    await db.commit()
    return {"lead": serialize_corporate_lead(lead)}


@router.delete(
    "/vendor/{lead_id}",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def delete_vendor_lead(lead_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    lead = await db.get(VendorLead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise HTTPException(status_code=404, detail="lead not found")
    lead.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True}


@router.delete("/corporate/{lead_id}", dependencies=[Depends(require_role("owner", "ops"))])
async def delete_corporate_lead(lead_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    lead = await db.get(CorporateLead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise HTTPException(status_code=404, detail="lead not found")
    lead.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True}


@router.post(
    "/vendor/{lead_id}/convert",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def convert_vendor_lead(lead_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Create a Vendor pre-filled from the lead and mark the lead 'live'."""
    lead = await db.get(VendorLead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    lead_categories = lead.categories or []
    # Slugify each lead category and ensure a vendor_categories row exists, so
    # ALL of the lead's categories carry over (not just the first).
    category_slugs: list[str] = []
    for raw in lead_categories:
        slug = _slugify_category(raw)
        if slug in category_slugs:
            continue
        category_slugs.append(slug)
        if await db.get(VendorCategory, slug) is None:
            db.add(VendorCategory(id=slug, name=str(raw).strip().title(), auto_created=True))

    primary = category_slugs[0] if category_slugs else "uncategorised"

    # Best-effort resolve free-text cities_raw → verified city_ids (exact id /
    # name / alias match only; no auto-create). Unmatched names stay in notes.
    matched_city_ids, unmatched = await _resolve_lead_cities(db, lead.cities_raw)

    note_extra = f" Unmatched cities: {', '.join(unmatched)}." if unmatched else ""
    vendor = Vendor(
        id=generate_id("ven"),
        name=lead.brand_name,
        category=primary,
        poc_name=lead.contact_name,
        poc_email=lead.email,
        phone=lead.phone,
        contact_info=(
            f"From lead {lead.id}. Contact: {lead.contact_name} <{lead.email}>. "
            f"Cities: {lead.cities_raw}.{note_extra} {lead.notes or ''}"
        ).strip(),
        onboarding_status="contacted",
        active=False,  # not live until KYC + products are in
    )
    db.add(vendor)
    await db.flush()
    for slug in category_slugs:
        db.add(VendorCategoryLink(vendor_id=vendor.id, category_id=slug))
    for city_id in matched_city_ids:
        db.add(VendorCity(vendor_id=vendor.id, city_id=city_id))
    lead.status = "live"
    await db.commit()
    return {"vendor_id": vendor.id, "lead": serialize_vendor_lead(lead)}


async def _resolve_lead_cities(
    db: AsyncSession, cities_raw: str | None
) -> tuple[list[str], list[str]]:
    """Match comma-separated free-text city names to verified city_ids.

    Returns (matched_city_ids, unmatched_names). Exact match on id / name /
    alias only — never auto-creates cities.
    """
    if not cities_raw or not cities_raw.strip():
        return [], []
    verified = (
        await db.execute(select(City).where(City.status == "verified"))
    ).scalars().all()
    lookup: dict[str, str] = {}
    for c in verified:
        lookup[c.id.lower()] = c.id
        lookup[c.name.lower()] = c.id
        for alias in c.aliases or []:
            lookup[alias.lower()] = c.id

    matched: list[str] = []
    unmatched: list[str] = []
    for name in cities_raw.split(","):
        key = name.strip().lower()
        if not key:
            continue
        city_id = lookup.get(key)
        if city_id and city_id not in matched:
            matched.append(city_id)
        elif not city_id:
            unmatched.append(name.strip())
    return matched, unmatched


class ActivityCreatePayload(BaseModel):
    type: str
    body: str | None = Field(default=None, max_length=8000)
    next_follow_up_at: datetime | None = None


class ActivityUpdatePayload(BaseModel):
    body: str | None = Field(default=None, max_length=8000)
    next_follow_up_at: datetime | None = None


@router.get(
    "/{kind}/{lead_id}/activities",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def list_activities(
    kind: str, lead_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    if kind not in _LEAD_MODELS:
        raise HTTPException(status_code=404, detail="unknown lead kind")
    activities = (
        await db.execute(
            select(LeadActivity)
            .where(LeadActivity.lead_kind == kind, LeadActivity.lead_id == lead_id)
            .order_by(LeadActivity.created_at.desc())
        )
    ).scalars().all()
    return {"activities": [serialize_activity(a) for a in activities]}


@router.post(
    "/{kind}/{lead_id}/activities",
    status_code=201,
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def create_activity(
    kind: str,
    lead_id: str,
    payload: ActivityCreatePayload,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    model = _LEAD_MODELS.get(kind)
    if model is None:
        raise HTTPException(status_code=404, detail="unknown lead kind")
    if payload.type not in ACTIVITY_TYPES:
        raise HTTPException(status_code=400, detail="invalid activity type")

    lead = await db.get(model, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")

    activity = LeadActivity(
        id=generate_id("lact"),
        lead_kind=kind,
        lead_id=lead_id,
        type=payload.type,
        body=payload.body,
        next_follow_up_at=payload.next_follow_up_at,
        created_by=x_admin_email,
    )
    db.add(activity)
    lead.next_follow_up_at = payload.next_follow_up_at
    await db.commit()
    return {"activity": serialize_activity(activity)}


@router.patch(
    "/{kind}/{lead_id}/activities/{activity_id}",
    dependencies=[Depends(require_role("owner", "ops", "vendor_success"))],
)
async def update_activity(
    kind: str,
    lead_id: str,
    activity_id: str,
    payload: ActivityUpdatePayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if kind not in _LEAD_MODELS:
        raise HTTPException(status_code=404, detail="unknown lead kind")

    activity = await db.get(LeadActivity, activity_id)
    if activity is None or activity.lead_kind != kind or activity.lead_id != lead_id:
        raise HTTPException(status_code=404, detail="activity not found")

    if payload.body is not None:
        activity.body = payload.body
    if payload.next_follow_up_at is not None or "next_follow_up_at" in payload.model_fields_set:
        activity.next_follow_up_at = payload.next_follow_up_at
        # Keep the lead's denormalized follow-up in sync only if this is the
        # most recent activity — otherwise an edit to an old entry would
        # incorrectly override a newer follow-up date.
        latest = (
            await db.execute(
                select(LeadActivity)
                .where(LeadActivity.lead_kind == kind, LeadActivity.lead_id == lead_id)
                .order_by(LeadActivity.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and latest.id == activity.id:
            model = _LEAD_MODELS[kind]
            lead = await db.get(model, lead_id)
            if lead is not None:
                lead.next_follow_up_at = payload.next_follow_up_at

    await db.commit()
    return {"activity": serialize_activity(activity)}
