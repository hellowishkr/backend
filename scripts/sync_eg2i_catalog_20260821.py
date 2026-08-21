"""Seed or synchronise the EG2I catalogue received on 2026-08-20.

Safety properties:

* validates the audited JSON checksum and exactly 150 unique products;
* touches only the resolved EG2I vendor and deterministic EG2I product IDs;
* creates or updates products idempotently and never deletes other rows;
* defaults to a transaction that is rolled back;
* requires ``--commit`` to persist and ``--publish`` to make listings visible;
* can pause a seasonal occasion without touching the rest of EG2I's catalogue;
* invalidates the storefront product cache after a committed change.

The supplied prices are stored as the selling/base price. EG2I still needs to
confirm that every catalogue price is GST-inclusive, so ``gst_rate`` and HSN
remain unset. Inventory is intentionally untracked (NULL).
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


EXPECTED_DATA_SHA256 = "0874f60716277ec6e96eeeaa7aa5bbf8730ce1cef730e8948832eefc60bfdd38"
EXPECTED_PRODUCTS = 150
DEFAULT_DATA_PATH = Path(__file__).with_name("data") / "eg2i_catalogue_20260820.json"
CANONICAL_VENDOR_ID = "ven_eg2i"
ALLOWED_CATEGORIES = {"Flowers", "Cakes", "Chocolates", "Hampers"}


def load_local_database_url() -> None:
    """Use .env.dev locally while respecting an injected container URL."""
    if os.environ.get("DATABASE_URL"):
        return
    env_dev = Path(__file__).resolve().parents[1] / ".env.dev"
    if not env_dev.exists():
        return
    for line in env_dev.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1].strip().strip("\"'")
            return


load_local_database_url()

from app.api.catalog import invalidate_product_cache
from app.db.session import async_session
from app.models.product import Product
from app.models.vendor import Vendor
from app.utils.html_sanitize import sanitize_html


@dataclass(frozen=True)
class CatalogueProduct:
    sku: str
    name: str
    description: str
    category: str
    occasions: tuple[str, ...]
    price_paise: int
    images: tuple[str, ...]
    source_catalogue: str
    seasonal: bool
    qa_flags: str


@dataclass(frozen=True)
class Catalogue:
    vendor: dict[str, Any]
    products: tuple[CatalogueProduct, ...]


@dataclass
class SyncSummary:
    vendor_id: str = ""
    vendor_created: bool = False
    products_created: int = 0
    products_updated: int = 0
    products_unchanged: int = 0
    field_changes: dict[str, int] = field(default_factory=dict)

    def changed(self, field_name: str) -> None:
        self.field_changes[field_name] = self.field_changes.get(field_name, 0) + 1

    @property
    def total_changes(self) -> int:
        return self.products_created + sum(self.field_changes.values())


def product_id_for_sku(sku: str) -> str:
    """Return a stable product ID that fits the 30-character database column."""
    normalized = re.sub(r"[^a-z0-9]+", "", sku.casefold())
    suffix = normalized[4:] if normalized.startswith("eg2i") else normalized
    candidate = f"prd_eg2i_{suffix}"
    if len(candidate) <= 30:
        return candidate
    digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()[:16]
    return f"prd_eg2i_{digest}"


def _validated_string(value: Any, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return result


def load_catalogue(path: Path = DEFAULT_DATA_PATH) -> Catalogue:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if path.resolve() == DEFAULT_DATA_PATH.resolve() and digest != EXPECTED_DATA_SHA256:
        raise ValueError(
            "EG2I catalogue checksum mismatch: "
            f"expected {EXPECTED_DATA_SHA256}, got {digest}"
        )

    payload = json.loads(raw)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported EG2I catalogue schema version")
    if payload.get("expected_products") != EXPECTED_PRODUCTS:
        raise ValueError("EG2I catalogue metadata does not expect 150 products")

    products: list[CatalogueProduct] = []
    seen_skus: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(payload.get("products", []), start=1):
        sku = _validated_string(item.get("sku"), field_name=f"product {index} SKU", max_length=80)
        sku_key = sku.casefold()
        if sku_key in seen_skus:
            raise ValueError(f"duplicate EG2I SKU: {sku}")
        seen_skus.add(sku_key)

        product_id = product_id_for_sku(sku)
        if product_id in seen_ids:
            raise ValueError(f"product ID collision derived from EG2I SKU: {sku}")
        seen_ids.add(product_id)

        category = _validated_string(
            item.get("category"), field_name=f"product {index} category", max_length=60
        )
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"unsupported category {category!r} for SKU {sku}")

        price_paise = item.get("price_paise")
        if not isinstance(price_paise, int) or price_paise <= 0:
            raise ValueError(f"invalid price for EG2I SKU {sku}")
        images = tuple(
            _validated_string(url, field_name=f"product {index} image", max_length=500)
            for url in item.get("images", [])
        )
        if not images or any(not url.startswith("https://") for url in images):
            raise ValueError(f"EG2I SKU {sku} must have at least one HTTPS image")

        products.append(
            CatalogueProduct(
                sku=sku,
                name=_validated_string(
                    item.get("name"), field_name=f"product {index} name", max_length=160
                ),
                description=_validated_string(
                    item.get("description"),
                    field_name=f"product {index} description",
                    max_length=20_000,
                ),
                category=category,
                occasions=tuple(
                    _validated_string(
                        occasion,
                        field_name=f"product {index} occasion",
                        max_length=60,
                    )
                    for occasion in item.get("occasions", [])
                ),
                price_paise=price_paise,
                images=images,
                source_catalogue=_validated_string(
                    item.get("source_catalogue"),
                    field_name=f"product {index} source catalogue",
                    max_length=40,
                ),
                seasonal=bool(item.get("seasonal")),
                qa_flags=str(item.get("qa_flags") or "").strip(),
            )
        )

    if len(products) != EXPECTED_PRODUCTS:
        raise ValueError(f"expected {EXPECTED_PRODUCTS} EG2I products, found {len(products)}")
    vendor = payload.get("vendor")
    if not isinstance(vendor, dict):
        raise ValueError("EG2I vendor metadata is missing")
    return Catalogue(vendor=vendor, products=tuple(products))


def _set_if_changed(summary: SyncSummary, target: Any, field_name: str, value: Any) -> bool:
    if getattr(target, field_name) == value:
        return False
    setattr(target, field_name, value)
    summary.changed(field_name)
    return True


async def resolve_vendor(
    db: AsyncSession,
    catalogue: Catalogue,
    *,
    publish: bool,
    summary: SyncSummary,
) -> Vendor:
    vendor = await db.get(Vendor, CANONICAL_VENDOR_ID)
    named_matches = (
        await db.execute(
            select(Vendor).where(func.lower(Vendor.name) == "eg2i")
        )
    ).scalars().all()

    if vendor is None:
        if len(named_matches) > 1:
            raise RuntimeError(f"found {len(named_matches)} vendors named EG2I")
        if named_matches:
            vendor = named_matches[0]
        else:
            vendor = Vendor(id=CANONICAL_VENDOR_ID, name="EG2I", category="Flowers")
            db.add(vendor)
            summary.vendor_created = True
    elif any(match.id != vendor.id for match in named_matches):
        raise RuntimeError("canonical EG2I vendor ID conflicts with another vendor named EG2I")

    summary.vendor_id = vendor.id
    metadata = catalogue.vendor
    desired = {
        "name": metadata["display_name"],
        "company_name": metadata["company_name"],
        "company_address": metadata["company_address"],
        "category": metadata["primary_category"],
        "vendor_type": "normal",
        "phone": metadata["phone"],
        "poc_name": metadata["poc_name"],
        "poc_email": metadata["poc_email"],
        "fulfillment_mode": "manual",
        "delivery_arrangement": "self",
        "delivery_policy": (
            "Standard and same-day delivery are free. Same-day orders placed before "
            "6 PM are delivered by 10 PM. Midnight delivery costs ₹300. Fixed-time "
            "delivery costs ₹200 for one-hour slots from 9 AM to 6 PM and requires "
            "two hours' advance booking."
        ),
        "return_policy": (
            "Replacement is considered after confirmation from the buyer or recipient. "
            "Complaint resolution may include a complimentary item, replacement, or refund."
        ),
        "cancellation_policy": (
            "Cancellation is allowed until 24 hours before delivery and is unavailable "
            "after dispatch or a delivery attempt."
        ),
        "delivery_lead_time_hours": 4,
        "same_day_cutoff_hour": 18,
        "min_price": min(product.price_paise for product in catalogue.products) // 100,
        "max_price": max(product.price_paise for product in catalogue.products) // 100,
        "reliability_score": 0.5,
        "is_national": True,
        "active": publish,
        "onboarding_status": "live" if publish else "kyc_done",
    }
    for field_name, value in desired.items():
        _set_if_changed(summary, vendor, field_name, value)
    await db.flush()
    return vendor


async def sync_catalogue(
    db: AsyncSession,
    catalogue: Catalogue,
    *,
    publish: bool,
    disabled_occasions: frozenset[str] = frozenset(),
) -> SyncSummary:
    summary = SyncSummary()
    vendor = await resolve_vendor(db, catalogue, publish=publish, summary=summary)

    existing_products = (
        await db.execute(select(Product).where(Product.vendor_id == vendor.id))
    ).scalars().all()
    products_by_id = {product.id: product for product in existing_products}

    for source in catalogue.products:
        disabled_for_occasion = bool(disabled_occasions.intersection(source.occasions))
        product_active = publish and not disabled_for_occasion
        product_id = product_id_for_sku(source.sku)
        product = products_by_id.get(product_id)
        created = product is None
        if product is None:
            collision = await db.get(Product, product_id)
            if collision is not None:
                raise RuntimeError(
                    f"EG2I product ID {product_id} belongs to vendor {collision.vendor_id}"
                )
            product = Product(
                id=product_id,
                vendor_id=vendor.id,
                name=source.name,
                base_price_paise=source.price_paise,
                variants=[],
                addons=[],
            )
            db.add(product)
            products_by_id[product_id] = product
            summary.products_created += 1

        desired = {
            "name": source.name,
            "description": sanitize_html(source.description),
            "category": source.category,
            "occasions": list(source.occasions),
            "preparation_time": None,
            "dispatch_lead_time_days": None,
            "delivery_pincodes": [],
            "is_bestseller": False,
            "is_customisable": False,
            "supports_gift_message": True,
            "ships_nationally": True,
            "product_images": list(source.images),
            "care_instructions": None,
            "whats_included": None,
            "customization_schema": [],
            "base_price_paise": source.price_paise,
            "discounted_price_paise": None,
            "inventory_quantity": None,
            "gst_rate": None,
            "hsn": None,
            "shipping_included": True,
            "shipping_fee_paise": None,
            "image_url": source.images[0],
            "video_url": None,
            "active": product_active,
            "in_stock": True,
            "lifecycle_status": (
                "active" if product_active else "paused" if publish else "draft"
            ),
        }
        changed = False
        for field_name, value in desired.items():
            changed |= _set_if_changed(summary, product, field_name, value)
        if not created:
            if changed:
                summary.products_updated += 1
            else:
                summary.products_unchanged += 1

    await db.flush()
    return summary


def print_summary(
    summary: SyncSummary,
    *,
    committed: bool,
    publish: bool,
    disabled_occasions: frozenset[str],
) -> None:
    print("EG2I_SYNC_BEGIN")
    print(f"mode={'COMMIT' if committed else 'DRY RUN (ROLLBACK)'}")
    print(f"publish={str(publish).lower()}")
    print(f"disabled_occasions={','.join(sorted(disabled_occasions)) or 'none'}")
    print(f"vendor_id={summary.vendor_id}")
    print(f"vendor_created={str(summary.vendor_created).lower()}")
    print(f"source_products={EXPECTED_PRODUCTS}")
    print(f"products_created={summary.products_created}")
    print(f"products_updated={summary.products_updated}")
    print(f"products_unchanged={summary.products_unchanged}")
    print(f"total_changes={summary.total_changes}")
    for field_name, count in sorted(summary.field_changes.items()):
        print(f"field_changes.{field_name}={count}")
    print("EG2I_SYNC_END")


async def run(
    *,
    data_path: Path = DEFAULT_DATA_PATH,
    commit: bool,
    publish: bool,
    disabled_occasions: frozenset[str] = frozenset(),
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    invalidate_cache: bool = True,
) -> SyncSummary:
    catalogue = load_catalogue(data_path)
    async with session_factory() as db:
        try:
            summary = await sync_catalogue(
                db,
                catalogue,
                publish=publish,
                disabled_occasions=disabled_occasions,
            )
            if commit:
                await db.commit()
            else:
                await db.rollback()
        except Exception:
            await db.rollback()
            raise

    if commit and invalidate_cache:
        await invalidate_product_cache()
    print_summary(
        summary,
        committed=commit,
        publish=publish,
        disabled_occasions=disabled_occasions,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help="path to the audited EG2I catalogue JSON",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="persist the sync; without this flag the transaction is rolled back",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="preview or commit visible listings; otherwise rows are treated as drafts",
    )
    parser.add_argument(
        "--disable-occasion",
        action="append",
        default=[],
        metavar="SLUG",
        help=(
            "pause EG2I products tagged with this occasion while publishing; "
            "repeat for multiple occasions (for example: --disable-occasion rakhi)"
        ),
    )
    args = parser.parse_args()
    disabled_occasions = frozenset(
        occasion.strip().casefold()
        for occasion in args.disable_occasion
        if occasion.strip()
    )
    asyncio.run(
        run(
            data_path=args.data.resolve(),
            commit=args.commit,
            publish=args.publish,
            disabled_occasions=disabled_occasions,
        )
    )


if __name__ == "__main__":
    main()
