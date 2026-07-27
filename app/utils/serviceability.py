"""Per-product delivery serviceability.

A product is serviceable to a destination if ANY of these holds:

  1. The product ships nationally (``product.ships_nationally``).
  2. The destination city resolves (by name / alias) to one of the vendor's
     operating cities (``vendor.city_ids``).
  3. The destination pincode falls under a pincode prefix of one of the
     vendor's operating cities (``City.pincode_prefixes``).

City + pincode-prefix back each other up: a typo'd or unlisted city is still
caught by the pincode prefix, and a malformed pincode is still caught by the
city name. Coverage data (aliases, prefixes) lives on the City and is curated
centrally, so vendors only pick which cities they operate in — they never
enumerate pincodes.

For a LOCAL product, if the vendor has no operating cities set we cannot
determine coverage, so we treat it as NOT serviceable (fail safe) — better to
hide the product than accept an order we can't fulfil.

This module is read-only: unlike ``resolve_city_id`` it never auto-creates
cities, so checking serviceability has no write side effects.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.city import City


@dataclass
class ServiceabilityResult:
    serviceable: bool
    reason: str  # 'national' | 'city_match' | 'pincode_match' | 'out_of_area' | 'no_vendor_cities'


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _city_matches_destination(city: City, dest_city: str, dest_pincode: str) -> bool:
    """Whether ``dest_city``/``dest_pincode`` falls within this city's coverage."""
    dc = _norm(dest_city)
    if dc:
        if dc == city.id.lower() or dc == _norm(city.name):
            return True
        if any(dc == _norm(alias) for alias in (city.aliases or [])):
            return True
    pin = (dest_pincode or "").strip()
    if pin:
        for prefix in city.pincode_prefixes or []:
            if pin.startswith(str(prefix)):
                return True
    return False


async def check_product_serviceability(
    db: AsyncSession,
    *,
    ships_nationally: bool,
    vendor_city_ids: list[str],
    dest_city: str | None,
    dest_pincode: str | None,
) -> ServiceabilityResult:
    if ships_nationally:
        return ServiceabilityResult(True, "national")

    if not vendor_city_ids:
        return ServiceabilityResult(False, "no_vendor_cities")

    cities = (
        await db.execute(select(City).where(City.id.in_(vendor_city_ids)))
    ).scalars().all()

    for city in cities:
        if _city_matches_destination(city, dest_city or "", dest_pincode or ""):
            # Prefer the more specific reason for observability.
            reason = (
                "city_match"
                if _norm(dest_city)
                and (
                    _norm(dest_city) in {city.id.lower(), _norm(city.name)}
                    or any(_norm(dest_city) == _norm(a) for a in (city.aliases or []))
                )
                else "pincode_match"
            )
            return ServiceabilityResult(True, reason)

    return ServiceabilityResult(False, "out_of_area")
