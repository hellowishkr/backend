"""City resolution — always returns a valid city_id for non-empty input.

Tiered: exact match → fuzzy match → race-safe auto-create pending row.
Preserves the user's typed form (truncated to storage limit) separately.
"""

import asyncio
import difflib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import City

logger = logging.getLogger("wishkr.cities")

# Matches the `city_raw` column width on users/contacts. Keep these in sync
# with the models if the column is ever widened.
CITY_RAW_MAX = 60

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FUZZY_CUTOFF = 0.82


@dataclass
class CityResolution:
    city_id: str | None
    city_raw: str | None
    match_kind: str  # 'exact' | 'fuzzy' | 'auto_created' | 'empty'


def _slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:40] or "city"


def _truncate_raw(text: str) -> str:
    return text[:CITY_RAW_MAX]


# In-process cache of verified cities with TTL. Small N (<500 expected),
# loaded once per TTL window per worker. Invalidated on auto-create paths
# don't matter (auto-created rows are pending, not verified).
_CACHE_TTL_S = 60.0
_cache: dict[str, Any] = {"loaded_at": 0.0, "rows": []}


async def _load_verified(db: AsyncSession) -> list[City]:
    now = time.monotonic()
    if now - _cache["loaded_at"] < _CACHE_TTL_S and _cache["rows"]:
        return _cache["rows"]  # type: ignore[return-value]
    rows = (await db.execute(select(City).where(City.status == "verified"))).scalars().all()
    _cache["rows"] = list(rows)
    _cache["loaded_at"] = now
    return _cache["rows"]  # type: ignore[return-value]


def invalidate_verified_cache() -> None:
    """Call from admin paths that promote pending → verified."""
    _cache["loaded_at"] = 0.0
    _cache["rows"] = []


async def resolve_city_id(db: AsyncSession, text: str | None) -> CityResolution:
    if not text:
        return CityResolution(None, None, "empty")
    raw = text.strip()
    if not raw:
        return CityResolution(None, None, "empty")

    raw_stored = _truncate_raw(raw)
    t = raw.lower()

    verified = await _load_verified(db)

    # Tier 1: exact match on id, name, or aliases
    for c in verified:
        if c.id.lower() == t or c.name.lower() == t:
            return CityResolution(c.id, raw_stored, "exact")
        for alias in c.aliases or []:
            if alias.lower() == t:
                return CityResolution(c.id, raw_stored, "exact")

    # Tier 2: fuzzy match against names + aliases (CPU-bound → offload)
    candidates: dict[str, str] = {}
    for c in verified:
        candidates[c.name.lower()] = c.id
        for alias in c.aliases or []:
            candidates[alias.lower()] = c.id
    if candidates:
        match = await asyncio.to_thread(
            difflib.get_close_matches, t, list(candidates.keys()), 1, _FUZZY_CUTOFF
        )
        if match:
            return CityResolution(candidates[match[0]], raw_stored, "fuzzy")

    # Tier 3: race-safe auto-create a pending city.
    # Dedupe by slug across case/whitespace variants (prefer merging over hash-suffix).
    slug = _slugify(raw)
    stmt = (
        pg_insert(City)
        .values(
            id=slug,
            name=raw.title() if raw.islower() or raw.isupper() else raw,
            state=None,
            country=None,  # unknown — admin triage sets this during promotion
            aliases=None,
            status="pending",
            auto_created=True,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.execute(stmt)
    await db.flush()

    # Whether insert won or lost the race, the row exists. If the existing row
    # is a *verified* city that happens to share the slug, merge into it (no
    # hash-suffix collision pile-up).
    existing = await db.get(City, slug)
    if existing is not None:
        logger.info("city_resolved slug=%s kind=auto_created winner=%s", slug, existing.status)
        return CityResolution(slug, raw_stored, "auto_created")

    # Extremely unlikely fallback: race + row deleted between our insert and re-read.
    # Use a deterministic suffix so retries converge.
    suffixed = f"{slug}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"
    await db.execute(
        pg_insert(City)
        .values(
            id=suffixed, name=raw_stored, state=None, country=None,
            aliases=None, status="pending", auto_created=True,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await db.flush()
    return CityResolution(suffixed, raw_stored, "auto_created")
