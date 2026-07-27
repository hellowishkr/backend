"""SourceAdapter protocol — every fulfillment source implements this."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ProviderCapabilities:
    """What a provider adapter can do today."""

    catalog_search: bool = True
    quote_validation: bool = False
    direct_order: bool = False
    manual_fulfillment: bool = False
    cancellation: bool = False
    refunds: bool = False


@dataclass
class ProviderPolicy:
    """Provider/vendor policy data used by deterministic ranking and checkout."""

    fulfillment_mode: str = "manual"       # api | manual | catalog_only
    delivery_policy: str | None = None
    return_policy: str | None = None
    cancellation_policy: str | None = None
    lead_time_hours: int | None = None
    same_day_cutoff_hour: int | None = None
    serviceable: bool = True
    serviceability_reason: str | None = None
    provider_health: str = "unknown"       # healthy | degraded | disabled | unknown
    reliability_score: float = 0.5


@dataclass
class ProviderQuote:
    """Provider-neutral checkout quote for a selected candidate."""

    provider_id: str
    provider_product_id: str
    amount: int
    currency: str = "INR"
    valid_until: datetime | None = None
    available: bool = True
    reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class GiftCandidate:
    """Unified shape across Hubble / Vendor / future partners."""

    source_id: str                       # "hubble", "vendor_db", "fnp_api"
    item_id: str                         # source-specific identifier
    name: str                            # "Cult.fit", "Sky Jumper Pune"
    vibes: list[str]                     # which taxonomy vibes this hits
    price_min: int                       # rupees; 0 if booking-based
    price_max: int                       # rupees; 0 if booking-based / open-priced
    fulfillment: str                     # "instant_voucher" | "physical_delivery" | "booking" | "experience_voucher"
    cities: list[str] | None = None      # None = pan-India coverage
    metadata: dict = field(default_factory=dict)
    policy: ProviderPolicy = field(default_factory=ProviderPolicy)
    image_url: str | None = None
    contact_info: str | None = None      # for vendor partners without API
    already_taken: bool = False          # set by ranker, not source


class SourceAdapter(Protocol):
    """Implement this to add a new fulfillment source."""

    source_id: str
    capabilities: ProviderCapabilities

    async def list_candidates(
        self,
        db: AsyncSession,
        vibes: list[str],
        budget_min: int | None,
        budget_max: int | None,
        city: str | None,
    ) -> list[GiftCandidate]:
        """Return candidates that match any of the requested vibes."""
        ...

    async def quote(
        self,
        db: AsyncSession,
        candidate: GiftCandidate,
        amount: int,
    ) -> ProviderQuote:
        """Validate a checkout amount for a selected candidate."""
        ...
