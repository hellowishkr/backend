"""Daily sync of partner catalogues into `gift_catalog`.

Runs at 06:00 IST. Fetches Hubble's live catalogue (all pages), upserts
rows, and marks previously-seen rows not present in this run as inactive.

Why a mirror instead of a live call?
- `suggest_gifts` used to call Hubble live inside the 15s WhatsApp turn
  budget. Cold paths paged 4x and hit the 15s fallback, so users saw
  "Sorry, something went wrong" even though the agent worked.
- With a local table, suggest_gifts answers in ~50ms regardless of
  partner count. Adding Xoxoday / local partners is a one-method change.

Marketplaces (Amazon, Flipkart, Myntra, …) get `is_marketplace=True` so
the bot can pivot "suggest something from Amazon" → Amazon gift card
without hallucinating product-level options we don't actually have.

Money movement (order creation) still calls the live partner API —
never trust cached denoms for payments.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import async_session
from app.models.gift_catalog import GiftCatalogItem
from app.services.hubble import hubble_service

logger = logging.getLogger("wishkr.sync_catalog")

# Brand-name substrings that mark an item as a generic online marketplace.
# These shouldn't be suggested as a concrete gift idea — instead, the bot
# offers them when the user asks for product-level discovery on a site we
# don't integrate (e.g. "suggest something from Amazon").
_MARKETPLACE_FRAGMENTS = (
    "amazon",
    "flipkart",
    "myntra",
    "ajio",
    "nykaa",
    "tata cliq",
    "meesho",
    "reliance digital",
    "croma",
)


def _is_marketplace(brand_title: str) -> bool:
    t = brand_title.lower()
    return any(frag in t for frag in _MARKETPLACE_FRAGMENTS)


async def sync_gift_catalog() -> dict:
    """Fetch Hubble catalogue live and upsert into gift_catalog.

    Returns a small summary dict for logging / ops visibility.
    """
    started = datetime.now(timezone.utc)
    try:
        brands = await hubble_service.get_brands(force_refresh=True)
    except Exception as e:
        logger.exception("catalog_sync_failed partner=hubble error=%s", e)
        return {"ok": False, "partner": "hubble", "error": str(e)}

    if not brands:
        logger.warning("catalog_sync_empty partner=hubble")
        return {"ok": True, "partner": "hubble", "upserted": 0, "deactivated": 0}

    seen_external_ids: list[str] = []
    upserted = 0

    async with async_session() as db:
        for b in brands:
            ext_id = str(b.get("id") or "").strip()
            title = (b.get("title") or "").strip()
            if not ext_id or not title:
                continue

            amount = b.get("amountRestrictions", {}) or {}
            min_amt = amount.get("minAmount")
            max_amt = amount.get("maxAmount")

            # Hubble returns `category` as a list of tag strings
            # (e.g. ["NEW_BRANDS", "FURNISHING", "HOT_DEALS"]) but the
            # gift_catalog.category column is VARCHAR(80). Normalise to
            # a comma-joined string so downstream filters can still
            # substring-match any tag. Tolerates the legacy case where
            # the upstream payload shape could be a single string.
            raw_cat = b.get("category")
            if isinstance(raw_cat, list):
                category = ",".join(str(c) for c in raw_cat if c)[:80] or None
            else:
                category = (str(raw_cat) if raw_cat else "")[:80] or None

            values = {
                "partner": "hubble",
                "external_id": ext_id,
                "brand_name": title[:200],
                "brand_name_lower": title.lower()[:200],
                "category": category,
                "image_url": (b.get("imageUrl") or "")[:500] or None,
                "min_amount": int(min_amt) if isinstance(min_amt, (int, float)) else None,
                "max_amount": int(max_amt) if isinstance(max_amt, (int, float)) else None,
                "is_marketplace": _is_marketplace(title),
                "active": True,
                "raw": b,
                "synced_at": started,
            }

            # Deterministic id built from the natural key so two concurrent
            # calls (or a single sync churning thousands of brands in one
            # second) can't collide on the primary key. Previously we used
            # ``generate_id("gc")`` — 4 random chars × 36 alphabet = 1.68M
            # combos, which under high insert-rate produced birthday-paradox
            # collisions and dumped ``IntegrityError: duplicate key value
            # violates unique constraint "gift_catalog_pkey"`` into Sentry.
            #
            # Using a short blake2 hash of (partner, external_id) gives a
            # bounded 16-hex-char id that fits the 40-char column no matter
            # how long external_id grows, stays stable across syncs, and
            # extends cleanly when we add Xoxoday / local partners — the
            # partner is mixed into the hash input so the same external_id
            # across two partners never collides.
            natural_key = f"hubble:{ext_id}".encode()
            deterministic_id = "gc_" + hashlib.blake2b(natural_key, digest_size=8).hexdigest()

            stmt = pg_insert(GiftCatalogItem).values(
                id=deterministic_id,
                **values,
            )
            # Natural key for upsert — (partner, external_id) — lets us keep
            # the synthetic `id` stable across syncs.
            stmt = stmt.on_conflict_do_update(
                index_elements=["partner", "external_id"],
                set_={
                    "brand_name": stmt.excluded.brand_name,
                    "brand_name_lower": stmt.excluded.brand_name_lower,
                    "category": stmt.excluded.category,
                    "image_url": stmt.excluded.image_url,
                    "min_amount": stmt.excluded.min_amount,
                    "max_amount": stmt.excluded.max_amount,
                    "is_marketplace": stmt.excluded.is_marketplace,
                    "active": True,
                    "raw": stmt.excluded.raw,
                    "synced_at": started,
                },
            )
            await db.execute(stmt)
            seen_external_ids.append(ext_id)
            upserted += 1

        # Deactivate rows we didn't see this run — Hubble removed or paused.
        # SAFETY: Hubble's pagination ends on an under-full page; a transient
        # mid-pagination blip can return fewer rows than expected and trick
        # us into deactivating still-live brands. Guardrails:
        #   * skip deactivation entirely if we saw 0 brands (handled above)
        #   * abort if we'd deactivate >25% of currently-active hubble rows;
        #     alert the founder so they can investigate before next run.
        deactivated = 0
        if seen_external_ids:
            # Count active rows we'd flip — refuse to deactivate the world.
            count_result = await db.execute(
                select(func.count(GiftCatalogItem.id))
                .where(
                    GiftCatalogItem.partner == "hubble",
                    GiftCatalogItem.active.is_(True),
                )
            )
            currently_active = int(count_result.scalar_one() or 0)
            would_deactivate_result = await db.execute(
                select(func.count(GiftCatalogItem.id))
                .where(
                    GiftCatalogItem.partner == "hubble",
                    GiftCatalogItem.active.is_(True),
                    GiftCatalogItem.external_id.notin_(seen_external_ids),
                )
            )
            would_deactivate = int(would_deactivate_result.scalar_one() or 0)

            if currently_active > 20 and would_deactivate * 4 > currently_active:
                logger.error(
                    "catalog_sync_safety_abort partner=hubble would_deactivate=%d active=%d "
                    "(>25%% — likely truncated pagination, not a real removal)",
                    would_deactivate, currently_active,
                )
                try:
                    from app.utils.alerts import send_founder_alert
                    await send_founder_alert(
                        f"⚠️ Hubble catalog sync aborted deactivation: "
                        f"would have flagged {would_deactivate}/{currently_active} brands "
                        f"as inactive (>25%). Likely a truncated pagination response."
                    )
                except Exception:
                    pass
            else:
                res = await db.execute(
                    update(GiftCatalogItem)
                    .where(
                        GiftCatalogItem.partner == "hubble",
                        GiftCatalogItem.active.is_(True),
                        GiftCatalogItem.external_id.notin_(seen_external_ids),
                    )
                    .values(active=False, synced_at=started)
                )
                deactivated = res.rowcount or 0

        await db.commit()

    logger.info(
        "catalog_sync_ok partner=hubble upserted=%d deactivated=%d",
        upserted, deactivated,
    )
    return {"ok": True, "partner": "hubble", "upserted": upserted, "deactivated": deactivated}
