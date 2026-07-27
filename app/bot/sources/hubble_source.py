"""HubbleSource — gift card aggregator. Reads from the local `gift_catalog`
mirror (populated nightly by `sync_gift_catalog`), filters by vibe (via
taxonomy) and budget.

Previously this called `hubble_service.get_brands()` live inside the
WhatsApp turn, which paged 4x and blew the 15s agent budget on cold
cache. Order placement still uses the live Hubble API.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gift_taxonomy import VIBE_TO_HUBBLE_BRANDS, hubble_titles_for_vibes
from app.bot.sources.base import GiftCandidate, ProviderCapabilities, ProviderPolicy, ProviderQuote
from app.models.gift_catalog import GiftCatalogItem

logger = logging.getLogger("wishkr.sources.hubble")


class HubbleSource:
    source_id = "hubble"
    capabilities = ProviderCapabilities(
        catalog_search=True,
        quote_validation=True,
        direct_order=True,
        manual_fulfillment=False,
        cancellation=False,
        refunds=False,
    )

    async def list_candidates(
        self,
        db: AsyncSession,
        vibes: list[str],
        budget_min: int | None,
        budget_max: int | None,
        city: str | None,
    ) -> list[GiftCandidate]:
        try:
            result = await db.execute(
                select(GiftCatalogItem).where(
                    GiftCatalogItem.partner == "hubble",
                    GiftCatalogItem.active.is_(True),
                )
            )
            brands = list(result.scalars().all())
        except Exception as e:
            logger.warning("Local Hubble catalogue read failed: %s", e)
            return []

        # Resolve requested vibes → flat set of title fragments
        wanted_titles = hubble_titles_for_vibes(vibes)
        # Build reverse map title-fragment → vibes (so we can tag candidates)
        fragment_to_vibes: dict[str, list[str]] = {}
        for v in vibes:
            for frag in VIBE_TO_HUBBLE_BRANDS.get(v, []):
                fragment_to_vibes.setdefault(frag.lower(), []).append(v)

        out: list[GiftCandidate] = []
        for b in brands:
            title = b.brand_name
            title_lower = b.brand_name_lower

            # Match vibe by substring on title
            matched_vibes: list[str] = []
            if wanted_titles:
                for frag, vlist in fragment_to_vibes.items():
                    if frag in title_lower:
                        matched_vibes.extend(vlist)
                if not matched_vibes:
                    continue

            min_amt = b.min_amount or 0
            max_amt = b.max_amount or 0

            # Budget intersection: candidate is in-budget if its range overlaps user's range
            if budget_max is not None and min_amt > budget_max:
                continue
            if budget_min is not None and max_amt and max_amt < budget_min:
                continue

            out.append(GiftCandidate(
                source_id=self.source_id,
                item_id=b.external_id,
                name=title,
                vibes=sorted(set(matched_vibes)),
                price_min=min_amt,
                price_max=max_amt,
                fulfillment="instant_voucher",
                cities=None,  # Hubble brands are pan-India
                metadata={
                    "category": b.category or "",
                    "raw_id": b.external_id,
                    "is_marketplace": b.is_marketplace,
                },
                policy=ProviderPolicy(
                    fulfillment_mode="api",
                    delivery_policy="Instant voucher delivery after payment, or scheduled delivery on occasion date for friend gifts.",
                    return_policy="Gift-card returns depend on provider/brand terms after voucher issuance.",
                    cancellation_policy="Payment link can be cancelled before payment; issued vouchers are generally non-cancellable.",
                    lead_time_hours=0,
                    serviceable=True,
                    provider_health="healthy",
                    reliability_score=0.8,
                ),
                image_url=b.image_url,
            ))

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
        if candidate.price_min and amount < candidate.price_min:
            return ProviderQuote(
                provider_id=self.source_id,
                provider_product_id=candidate.item_id,
                amount=amount,
                available=False,
                reason=f"Minimum amount is ₹{candidate.price_min}.",
            )
        if candidate.price_max and amount > candidate.price_max:
            return ProviderQuote(
                provider_id=self.source_id,
                provider_product_id=candidate.item_id,
                amount=amount,
                available=False,
                reason=f"Maximum amount is ₹{candidate.price_max}.",
            )
        return ProviderQuote(
            provider_id=self.source_id,
            provider_product_id=candidate.item_id,
            amount=amount,
            available=True,
        )
