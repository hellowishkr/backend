"""Mobile AI Visual Discovery & Gifting History API routes for wishkr_app.

Endpoints:
  POST /api/v1/recommendations/discover         — Curated AI gift discovery feed
  GET  /api/v1/contacts/{contact_id}/gifts      — Gifting history timeline for a contact
  POST /api/v1/contacts/{contact_id}/gifts      — "Log My Gift" for past celebrations
  POST /api/v1/recommendations/check-duplicate  — Anti-duplicate warning check
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
from app.models.contact import Contact
from app.models.gift_log import GiftLog
from app.models.product import Product
from app.models.user import User
from app.utils.id_gen import generate_id

logger = logging.getLogger("wishkr.mobile_discovery")

router = APIRouter(tags=["mobile_discovery"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class DiscoveryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None = None
    price: float
    image_url: str | None = None
    brand_store: str
    vibe: str
    why_this_fits: str
    product_url: str | None = None


class DiscoveryRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    vibe: str | None = Field(None, description="e.g. Specialty Coffee, Gamer, Fitness, Sneakerhead")
    relationship: str | None = Field(None, description="Friend, Spouse, Sibling, Colleague, Parent")
    budget_min: int | None = Field(0, ge=0)
    budget_max: int | None = Field(10000, ge=0)
    occasion_type: str | None = Field(None, description="birthday, anniversary, etc.")


class DiscoveryResponse(BaseModel):
    items: list[DiscoveryItem]
    count: int
    vibe_applied: str | None = None
    budget_applied: str | None = None


class GiftLogItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    contact_id: str
    description: str
    gift_year: str | None = None
    created_at: str


class CreateGiftLogPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str = Field(..., min_length=2, max_length=200, description="What gift was given")
    gift_year: str | None = Field(None, max_length=4, description="Year given e.g. '2025'")
    occasion_id: str | None = Field(None, description="Optional associated occasion ID")


class CheckDuplicatePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contact_id: str
    product_name: str


class CheckDuplicateResponse(BaseModel):
    is_duplicate: bool
    warning: str | None = None
    matched_gift: str | None = None
    matched_year: str | None = None


# ── Curated Catalog Baseline for Discovery ───────────────────────────────────

_CURATED_DISCOVERY_CATALOG = [
    {
        "id": "dsc_coffee_01",
        "title": "Timemore Chestnut C2 Manual Coffee Grinder",
        "description": "Precision conical burrs crafted for specialty pour-over and AeroPress enthusiasts.",
        "price": 4499.0,
        "image_url": "https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?w=600",
        "brand_store": "Blue Tokai Coffee",
        "vibe": "Specialty Coffee",
        "why_this_fits": "Essential upgrade for pour-over coffee lovers looking for barista grind consistency.",
        "keywords": ["coffee", "grinder", "pour-over", "timemore", "brew"],
    },
    {
        "id": "dsc_coffee_02",
        "title": "Hario V60 Ceramic Pour-Over Kit",
        "description": "Iconic Japanese ceramic dripper and server set for clean, aromatic single-origin coffee.",
        "price": 2490.0,
        "image_url": "https://images.unsplash.com/photo-1495474472287-4d71bcdd2085?w=600",
        "brand_store": "Amazon",
        "vibe": "Specialty Coffee",
        "why_this_fits": "Timeless starter setup for discovering specialty coffee flavors.",
        "keywords": ["coffee", "v60", "hario", "dripper"],
    },
    {
        "id": "dsc_gamer_01",
        "title": "8BitDo Ultimate Wireless Controller with Charging Dock",
        "description": "Hall-effect anti-drift joysticks with custom button mapping and multi-platform support.",
        "price": 4999.0,
        "image_url": "https://images.unsplash.com/photo-1600080972464-8e5f35f63d08?w=600",
        "brand_store": "Flipkart",
        "vibe": "Gamer",
        "why_this_fits": "Zero-drift hall sticks make this the highest-rated pro controller for PC & Switch.",
        "keywords": ["game", "gamer", "controller", "pc", "switch", "console"],
    },
    {
        "id": "dsc_gamer_02",
        "title": "Razer BlackShark V2 X Gaming Headset",
        "description": "HyperClear cardioid microphone with 7.1 surround sound audio immersion.",
        "price": 3999.0,
        "image_url": "https://images.unsplash.com/photo-1546435770-a3e426bf472b?w=600",
        "brand_store": "Amazon",
        "vibe": "Gamer",
        "why_this_fits": "Ultra-lightweight competitive gaming headset with crystal-clear voice comms.",
        "keywords": ["game", "gamer", "headset", "razer", "audio"],
    },
    {
        "id": "dsc_fitness_01",
        "title": "Theragun Mini Massage Gun 2.0",
        "description": "Ultra-portable percussion therapy muscle treatment designed for athletes and gym recovery.",
        "price": 8999.0,
        "image_url": "https://images.unsplash.com/photo-1518611012118-696072aa579a?w=600",
        "brand_store": "Nykaa Man",
        "vibe": "Fitness",
        "why_this_fits": "Compact deep tissue recovery tool for fitness routines and sore muscles.",
        "keywords": ["fitness", "gym", "massage", "theragun", "workout"],
    },
    {
        "id": "dsc_book_01",
        "title": "Kindle Paperwhite (16 GB, 6.8\" Display with Warm Light)",
        "description": "Glare-free 300 ppi paper-like display with adjustable warm light and weeks of battery life.",
        "price": 9999.0,
        "image_url": "https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=600",
        "brand_store": "Amazon",
        "vibe": "Minimalist",
        "why_this_fits": "The gold standard for avid readers who love carrying entire libraries anywhere.",
        "keywords": ["book", "reading", "kindle", "minimalist"],
    },
    {
        "id": "dsc_wellness_01",
        "title": "Forest Essentials Soundarya Beauty & Wellness Gift Box",
        "description": "Ayurvedic 24K gold serum and pure rosewater luxury facial rejuvenation hamper.",
        "price": 5200.0,
        "image_url": "https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?w=600",
        "brand_store": "Forest Essentials",
        "vibe": "Self Care",
        "why_this_fits": "Luxurious Ayurvedic self-care ritual for birthdays and anniversaries.",
        "keywords": ["skincare", "beauty", "self care", "wellness", "spa"],
    },
]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/v1/recommendations/discover", response_model=DiscoveryResponse)
async def discover_gift_recommendations(
    payload: DiscoveryRequestPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DiscoveryResponse:
    """Query AI curated gift recommendations tailored by vibe, relationship, and budget."""
    min_p = payload.budget_min or 0
    max_p = payload.budget_max or 25000
    target_vibe = (payload.vibe or "").strip().lower()

    # 1. First search active in-house marketplace products
    matched_items: list[DiscoveryItem] = []
    try:
        stmt = (
            select(Product)
            .where(Product.active.is_(True), Product.in_stock.is_(True))
            .limit(10)
        )
        products = list((await db.scalars(stmt)).all())
        for p in products:
            p_price = (p.discounted_price_paise or p.base_price_paise) / 100.0
            if min_p <= p_price <= max_p:
                cat = p.category or "Gifts"
                matched_items.append(
                    DiscoveryItem(
                        id=f"prd_{p.id}",
                        title=p.name,
                        description=p.description,
                        price=p_price,
                        image_url=p.image_url or (p.product_images[0] if p.product_images else None),
                        brand_store="Wishkr Marketplace",
                        vibe=cat,
                        why_this_fits=f"Handpicked from verified partners matching {cat}.",
                    )
                )
    except Exception as e:
        logger.debug("Database product lookup bypassed: %s", e)

    # 2. Enrich with curated discovery feed matching the vibe/budget criteria
    for item in _CURATED_DISCOVERY_CATALOG:
        price = item["price"]
        if min_p <= price <= max_p:
            vibe_str = item["vibe"].lower()
            keywords = [k.lower() for k in item["keywords"]]

            # If vibe filter is specified, ensure it matches vibe or keywords
            if target_vibe:
                if (target_vibe not in vibe_str) and not any(k in target_vibe for k in keywords):
                    continue

            matched_items.append(
                DiscoveryItem(
                    id=item["id"],
                    title=item["title"],
                    description=item["description"],
                    price=item["price"],
                    image_url=item["image_url"],
                    brand_store=item["brand_store"],
                    vibe=item["vibe"],
                    why_this_fits=item["why_this_fits"],
                )
            )

    budget_label = f"₹{min_p:,} – ₹{max_p:,}"
    logger.info(
        "User %s discovered %d gift ideas for vibe='%s', budget='%s'",
        user.id,
        len(matched_items),
        payload.vibe,
        budget_label,
    )

    return DiscoveryResponse(
        items=matched_items,
        count=len(matched_items),
        vibe_applied=payload.vibe,
        budget_applied=budget_label,
    )


@router.get("/v1/contacts/{contact_id}/gifts", response_model=list[GiftLogItem])
async def list_contact_gift_history(
    contact_id: str,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[GiftLogItem]:
    """Retrieve historical gifts given to a specific contact (Feature 9)."""
    stmt = (
        select(GiftLog)
        .where(
            GiftLog.user_id == user.id,
            GiftLog.contact_id == contact_id,
        )
        .order_by(GiftLog.gift_year.desc().nullslast(), GiftLog.created_at.desc())
    )
    logs = list((await db.scalars(stmt)).all())

    return [
        GiftLogItem(
            id=log.id,
            contact_id=log.contact_id,
            description=log.description,
            gift_year=log.gift_year,
            created_at=log.created_at.isoformat() if log.created_at else datetime.now(timezone.utc).isoformat(),
        )
        for log in logs
    ]


@router.post("/v1/contacts/{contact_id}/gifts", response_model=GiftLogItem, status_code=status.HTTP_201_CREATED)
async def log_gift_given(
    contact_id: str,
    payload: CreateGiftLogPayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GiftLogItem:
    """Log a gift given to a contact for future memory & anti-duplicate tracking."""
    contact = await db.scalar(
        select(Contact).where(Contact.id == contact_id, Contact.user_id == user.id)
    )
    if not contact:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")

    year_str = payload.gift_year or str(datetime.now(timezone.utc).year)
    new_log = GiftLog(
        id=generate_id("log"),
        user_id=user.id,
        contact_id=contact.id,
        occasion_id=payload.occasion_id,
        description=payload.description.strip(),
        gift_year=year_str,
    )
    db.add(new_log)
    await db.commit()
    await db.refresh(new_log)

    logger.info("User %s logged gift '%s' for contact %s (%s)", user.id, new_log.description, contact.id, year_str)
    return GiftLogItem(
        id=new_log.id,
        contact_id=new_log.contact_id,
        description=new_log.description,
        gift_year=new_log.gift_year,
        created_at=new_log.created_at.isoformat(),
    )


@router.post("/v1/recommendations/check-duplicate", response_model=CheckDuplicateResponse)
async def check_duplicate_gift(
    payload: CheckDuplicatePayload,
    user: Annotated[User, Depends(get_current_mobile_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CheckDuplicateResponse:
    """Check if a gift idea was previously given to this contact to prevent duplicate gifting."""
    stmt = (
        select(GiftLog)
        .where(
            GiftLog.user_id == user.id,
            GiftLog.contact_id == payload.contact_id,
        )
    )
    past_gifts = list((await db.scalars(stmt)).all())

    candidate_words = set(payload.product_name.lower().split())

    for past in past_gifts:
        past_words = set(past.description.lower().split())
        overlap = candidate_words.intersection(past_words)
        # Filter common filler words
        meaningful_overlap = {w for w in overlap if len(w) > 3}
        if meaningful_overlap:
            year_info = f" in {past.gift_year}" if past.gift_year else ""
            warning = f"You previously gave '{past.description}'{year_info} to this contact."
            return CheckDuplicateResponse(
                is_duplicate=True,
                warning=warning,
                matched_gift=past.description,
                matched_year=past.gift_year,
            )

    return CheckDuplicateResponse(is_duplicate=False)
