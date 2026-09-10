"""VendorSource — direct partners stored in the vendors table.

Mirrors the query pattern in functions.search_vendors: national vendors
always; local vendors when city_id matches.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_taxonomy import VIBE_TO_VENDOR_CATEGORIES, vendor_categories_for_vibes
from app.bot.sources.base import GiftCandidate, ProviderCapabilities, ProviderPolicy, ProviderQuote
from app.services.delivery_estimate import (
    express_delivery_available_today,
    supports_express_delivery,
)
from app.models import Vendor, VendorCategoryLink, VendorCity

logger = logging.getLogger("wishkr.sources.vendor")


# Map vendor category → fulfillment style
_VENDOR_FULFILLMENT: dict[str, str] = {
    "private_theatre": "booking",
    "experience": "experience_voucher",
    "spa": "experience_voucher",
    "salon": "experience_voucher",
    "wellness": "experience_voucher",
    "resort": "booking",
    "stay": "booking",
    "cake": "physical_delivery",
    "bakery": "physical_delivery",
    "florist": "physical_delivery",
    "flowers": "physical_delivery",
    "personalized_gift": "physical_delivery",
    "personalised_gift": "physical_delivery",
    "jewellery": "physical_delivery",
    "chocolates": "physical_delivery",
    "mithai": "physical_delivery",
    "sweets": "physical_delivery",
    "bags": "physical_delivery",
    "accessories": "physical_delivery",
    "plants": "physical_delivery",
    "nursery": "physical_delivery",
    "art": "physical_delivery",
    "skincare": "physical_delivery",
    "ethnic_wear": "physical_delivery",
    "religious": "physical_delivery",
    "pet": "physical_delivery",
    "games": "physical_delivery",
    "gift_card": "instant_voucher",
}


def vendor_gift_format(category: str | None) -> str | None:
    """Map the canonical vendor category to the user-facing gift format."""
    fulfillment = _VENDOR_FULFILLMENT.get((category or "").strip().lower())
    if fulfillment is None:
        return None
    if fulfillment in {"booking", "experience_voucher"}:
        return "experience"
    if fulfillment == "instant_voucher":
        return "digital"
    return "physical"


class VendorSource:
    source_id = "vendor_db"
    capabilities = ProviderCapabilities(
        catalog_search=True,
        quote_validation=False,
        direct_order=False,
        manual_fulfillment=True,
        cancellation=True,
        refunds=True,
    )

    async def list_candidates(
        self,
        db: AsyncSession,
        vibes: list[str],
        budget_min: int | None,
        budget_max: int | None,
        city: str | None,
    ) -> list[GiftCandidate]:
        wanted_categories = vendor_categories_for_vibes(vibes)
        if not wanted_categories:
            return []

        # Reverse map category → vibes (for tagging candidates)
        cat_to_vibes: dict[str, list[str]] = {}
        for v in vibes:
            for c in VIBE_TO_VENDOR_CATEGORIES.get(v, []):
                cat_to_vibes.setdefault(c.lower(), []).append(v)

        # Match on the primary category OR any linked category (multi-category
        # vendors surface when any of their categories is wanted).
        wanted = list(wanted_categories)
        base = select(Vendor).where(
            Vendor.active.is_(True),
            or_(
                Vendor.category.in_(wanted),
                Vendor.category_links.any(VendorCategoryLink.category_id.in_(wanted)),
            ),
        )

        # National vendors always; local only if we know the city
        if city:
            local_q = (
                base.join(VendorCity, VendorCity.vendor_id == Vendor.id)
                .where(VendorCity.city_id == city)
                .where(Vendor.is_national.is_(False))
            )
            national_q = base.where(Vendor.is_national.is_(True))
            local_rows = (await db.execute(local_q)).scalars().unique().all()
            national_rows = (await db.execute(national_q)).scalars().all()
            rows = list(national_rows) + list(local_rows)
        else:
            national_q = base.where(Vendor.is_national.is_(True))
            rows = list((await db.execute(national_q)).scalars().all())

        out: list[GiftCandidate] = []
        for vendor in rows:
            cat = (vendor.category or "").lower()
            matched_vibes = sorted(set(cat_to_vibes.get(cat, [])))
            out.append(
                GiftCandidate(
                    source_id=self.source_id,
                    item_id=vendor.id,
                    name=vendor.name,
                    vibes=matched_vibes,
                    price_min=0,  # vendor pricing is per-item; not in DB yet
                    price_max=0,
                    fulfillment=_VENDOR_FULFILLMENT.get(cat, "booking"),
                    cities=None if vendor.is_national else [city] if city else None,
                    metadata={"category": cat, "is_national": vendor.is_national},
                    policy=ProviderPolicy(
                        fulfillment_mode="manual",
                        delivery_policy=getattr(vendor, "delivery_policy", None),
                        return_policy=getattr(vendor, "return_policy", None),
                        cancellation_policy=getattr(vendor, "cancellation_policy", None),
                        lead_time_hours=getattr(vendor, "delivery_lead_time_hours", None),
                        same_day_cutoff_hour=getattr(vendor, "same_day_cutoff_hour", None),
                        express_delivery=supports_express_delivery(vendor),
                        express_delivery_available_today=express_delivery_available_today(vendor),
                        serviceable=True,
                        provider_health="healthy",
                        reliability_score=float(getattr(vendor, "reliability_score", None) or 0.5),
                    ),
                    contact_info=vendor.contact_info,
                )
            )
        return out

    async def quote(
        self,
        db: AsyncSession,
        candidate: GiftCandidate,
        amount: int,
    ) -> ProviderQuote:
        if amount <= 0:
            return ProviderQuote(
                provider_id=self.source_id,
                provider_product_id=candidate.item_id,
                amount=amount,
                available=False,
                reason="Amount must be greater than zero.",
            )
        return ProviderQuote(
            provider_id=self.source_id,
            provider_product_id=candidate.item_id,
            amount=amount,
            available=True,
            reason="Manual vendor quote; final availability is confirmed by Wishkr operations.",
        )
