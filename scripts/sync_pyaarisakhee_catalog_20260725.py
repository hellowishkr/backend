"""Synchronise PyariSakhee products from the authoritative 2026-07-25 workbook.

The script is deliberately production-safe:

* it accepts only the audited workbook SHA-256 and exactly 104 catalogue/SKU rows;
* it resolves the single intended vendor and aborts on ambiguous product matches;
* it updates selling price, MRP, exact inventory, stock, content, and occasions;
* it preserves stronger existing descriptions and Wishkr-hosted product images;
* it creates workbook products that do not yet exist;
* it never deletes or archives database products missing from the workbook;
* it is idempotent and defaults to a transaction that is rolled back;
* only ``--commit`` persists changes.

The schema migration ``b2f7e9c4a106`` must be applied before this script runs.
Null inventory is intentional and means unlimited, matching the workbook.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload


EXPECTED_WORKBOOK_SHA256 = (
    "ff83c811005461fe1ca32951f581f76cca3fb2d0e3ec3eab59a5239548dc75f7"
)
EXPECTED_CATALOGUE_ROWS = 104
DEFAULT_VENDOR_NAME = "PyariSakhee"

MANUAL_MATCH_OVERRIDES = {
    "e4e36bd9-331c-46fb-8fba-be86019f03e3": "prd_1783047464_6d9d714b",
    "b7f8d110-ed91-44e7-9b70-00959e4d07ee": "prd_1783047470_90317c8c",
}

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN_NS}


def load_local_database_url() -> None:
    """Use .env.dev locally while respecting an injected production URL."""
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
from app.models.product import Product, ProductVariant
from app.models.vendor import Vendor
from app.utils.html_sanitize import html_to_text, sanitize_html


@dataclass(frozen=True)
class CatalogueRow:
    row: int
    sku_id: str
    custom_sku: str | None
    name: str
    mrp_paise: int
    selling_paise: int
    product_category: str | None
    description: str | None
    variant_relationship: str | None
    size: str | None
    colour_code: str | None
    colour_name: str | None
    is_bestseller: bool
    hsn: str | None
    images: tuple[str, ...]


@dataclass(frozen=True)
class InventoryRow:
    sku_id: str
    quantity: int | None


@dataclass(frozen=True)
class SourceItem:
    catalogue: CatalogueRow
    inventory: InventoryRow


@dataclass(frozen=True)
class SourceGroup:
    items: tuple[SourceItem, ...]

    @property
    def primary(self) -> SourceItem:
        return self.items[0]

    @property
    def has_variants(self) -> bool:
        return any(
            (item.catalogue.variant_relationship or "").casefold() in {"parent", "child"}
            for item in self.items
        )

    @property
    def inventory_quantity(self) -> int | None:
        quantities = [item.inventory.quantity for item in self.items]
        if any(quantity is None for quantity in quantities):
            return None
        return sum(quantity for quantity in quantities if quantity is not None)


@dataclass
class SyncSummary:
    source_skus: int = 0
    source_products: int = 0
    products_created: int = 0
    products_updated: int = 0
    variants_created: int = 0
    variants_updated: int = 0
    variants_deactivated: int = 0
    field_changes: dict[str, int] = field(default_factory=dict)
    missing_from_workbook: list[tuple[str, str]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    def changed(self, field_name: str) -> None:
        self.field_changes[field_name] = self.field_changes.get(field_name, 0) + 1

    @property
    def total_changes(self) -> int:
        return (
            self.products_created
            + self.variants_created
            + self.variants_deactivated
            + sum(self.field_changes.values())
        )


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise ValueError(f"invalid cell reference: {reference}")
    result = 0
    for character in letters.group():
        result = result * 26 + ord(character) - 64
    return result


def _xlsx_rows(path: Path) -> dict[str, list[dict[int, Any]]]:
    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(text.text or "" for text in item.iterfind(".//m:t", NS))
                for item in root.findall("m:si", NS)
            ]

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            relationship.attrib["Id"]: relationship.attrib["Target"]
            for relationship in relationships
        }

        sheets: dict[str, list[dict[int, Any]]] = {}
        sheets_element = workbook.find("m:sheets", NS)
        if sheets_element is None:
            raise ValueError("workbook has no sheets")
        for sheet in sheets_element:
            relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
            target = targets[relationship_id]
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            root = ET.fromstring(archive.read(target))
            decoded_rows: list[dict[int, Any]] = []
            for row in root.findall(".//m:sheetData/m:row", NS):
                decoded: dict[int, Any] = {0: int(row.attrib["r"])}
                for cell in row.findall("m:c", NS):
                    column = _column_number(cell.attrib["r"])
                    cell_type = cell.attrib.get("t")
                    value_element = cell.find("m:v", NS)
                    if cell_type == "inlineStr":
                        inline = cell.find("m:is", NS)
                        value = (
                            "".join(
                                text.text or ""
                                for text in inline.iterfind(".//m:t", NS)
                            )
                            if inline is not None
                            else ""
                        )
                    elif value_element is None:
                        value = None
                    elif cell_type == "s":
                        value = shared_strings[int(value_element.text or "0")]
                    elif cell_type == "b":
                        value = value_element.text == "1"
                    else:
                        value = value_element.text
                    decoded[column] = value
                decoded_rows.append(decoded)
            sheets[sheet.attrib["name"]] = decoded_rows
        return sheets


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _money_to_paise(value: Any, *, label: str, row: int) -> int:
    text = _text(value)
    if text is None:
        raise ValueError(f"{label} is blank on catalogue row {row}")
    decimal = Decimal(text)
    paise = decimal * 100
    if paise != paise.to_integral_value() or paise < 0:
        raise ValueError(f"invalid {label} {text!r} on catalogue row {row}")
    return int(paise)


def _quantity(value: Any, *, row: int) -> int | None:
    text = _text(value)
    if text is None:
        return None
    decimal = Decimal(text)
    if decimal != decimal.to_integral_value() or decimal < 0:
        raise ValueError(f"invalid inventory {text!r} on inventory row {row}")
    return int(decimal)


def load_workbook(path: Path) -> list[SourceGroup]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_WORKBOOK_SHA256:
        raise RuntimeError(
            "workbook SHA-256 does not match the audited PyaariSakhee file: "
            f"expected {EXPECTED_WORKBOOK_SHA256}, got {digest}"
        )

    sheets = _xlsx_rows(path)
    catalogue_name = next(
        (name for name in sheets if name.casefold() in {"catalogue", "cataloque"}),
        None,
    )
    inventory_name = next(
        (name for name in sheets if name.casefold() == "inventory"),
        None,
    )
    if catalogue_name is None or inventory_name is None:
        raise ValueError(f"expected catalogue and inventory sheets; found {sorted(sheets)}")

    catalogue: list[CatalogueRow] = []
    for cells in sheets[catalogue_name]:
        row = cells[0]
        if row == 1:
            continue
        sku_id = _text(cells.get(1))
        name = _text(cells.get(4))
        if sku_id is None and name is None:
            continue
        if sku_id is None or name is None:
            raise ValueError(f"incomplete catalogue identity on row {row}")
        mrp = _money_to_paise(cells.get(5), label="MRP", row=row)
        selling = (
            _money_to_paise(cells.get(6), label="selling price", row=row)
            if _text(cells.get(6)) is not None
            else mrp
        )
        if selling > mrp:
            raise ValueError(f"selling price exceeds MRP for {name!r} on row {row}")
        catalogue.append(
            CatalogueRow(
                row=row,
                sku_id=sku_id,
                custom_sku=_text(cells.get(3)),
                name=name,
                mrp_paise=mrp,
                selling_paise=selling,
                product_category=_text(cells.get(8)),
                description=_text(cells.get(9)),
                variant_relationship=_text(cells.get(10)),
                size=_text(cells.get(11)),
                colour_code=_text(cells.get(12)),
                colour_name=_text(cells.get(13)),
                is_bestseller=(_text(cells.get(14)) or "").casefold() == "yes",
                hsn=_text(cells.get(15)),
                images=tuple(
                    image
                    for column in range(16, 22)
                    if (image := _text(cells.get(column))) is not None
                ),
            )
        )

    inventory: list[InventoryRow] = []
    for cells in sheets[inventory_name]:
        row = cells[0]
        if row == 1:
            continue
        sku_id = _text(cells.get(1))
        name = _text(cells.get(3))
        if sku_id is None and name is None:
            continue
        if sku_id is None or name is None:
            raise ValueError(f"incomplete inventory identity on row {row}")
        inventory.append(
            InventoryRow(sku_id=sku_id, quantity=_quantity(cells.get(8), row=row))
        )

    if len(catalogue) != EXPECTED_CATALOGUE_ROWS or len(inventory) != EXPECTED_CATALOGUE_ROWS:
        raise RuntimeError(
            "expected exactly 104 catalogue and inventory rows; got "
            f"{len(catalogue)} and {len(inventory)}"
        )
    catalogue_skus = [row.sku_id.casefold() for row in catalogue]
    inventory_skus = [row.sku_id.casefold() for row in inventory]
    if len(set(catalogue_skus)) != len(catalogue_skus):
        raise RuntimeError("duplicate catalogue SKU IDs found")
    if len(set(inventory_skus)) != len(inventory_skus):
        raise RuntimeError("duplicate inventory SKU IDs found")
    if set(catalogue_skus) != set(inventory_skus):
        raise RuntimeError("catalogue and inventory SKU sets do not match")

    inventory_by_sku = {row.sku_id.casefold(): row for row in inventory}
    groups: list[list[SourceItem]] = []
    for row in catalogue:
        item = SourceItem(row, inventory_by_sku[row.sku_id.casefold()])
        relationship = (row.variant_relationship or "").casefold()
        if relationship == "child":
            if not groups:
                raise RuntimeError(f"child variant has no parent on row {row.row}")
            if groups[-1][0].catalogue.name != row.name:
                raise RuntimeError(f"child variant name differs from parent on row {row.row}")
            groups[-1].append(item)
        else:
            groups.append([item])
    return [SourceGroup(tuple(items)) for items in groups]


def _compact_sku(sku_id: str) -> str:
    return re.sub(r"[^a-f0-9]", "", sku_id.casefold())


def product_id_for_sku(sku_id: str) -> str:
    return "prd_ps_" + _compact_sku(sku_id)[:20]


def variant_id_for_sku(sku_id: str) -> str:
    return "var_ps_" + _compact_sku(sku_id)[:20]


def variant_sku(sku_id: str) -> str:
    return "PYAARISAKHEE-" + _compact_sku(sku_id)[:16]


def _name_key(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", value)


def _image_key(value: str) -> str:
    return value.strip().split("?", 1)[0].casefold()


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _has_substantive_description(value: str | None, *, name: str) -> bool:
    """Reject blank, title-only, and very thin catalogue descriptions."""
    text = html_to_text(sanitize_html(value))
    if not text or _name_key(text) == _name_key(name):
        return False
    return len(text) >= 80 and len(text.split()) >= 12


def _generated_description(row: CatalogueRow) -> str:
    """Build a safe fallback when the workbook contains only a product title."""
    name = row.name.strip()
    lowered = name.casefold()
    if any(word in lowered for word in ("jhumka", "jhumki")):
        product_type = "a pair of traditional jhumka earrings"
    elif "chandbali" in lowered:
        product_type = "a pair of traditional chandbali earrings"
    elif any(word in lowered for word in ("earring", "stud", "dangler")):
        product_type = "a pair of fashion earrings"
    elif "ring" in lowered:
        product_type = "a fashion ring"
    elif any(word in lowered for word in ("bracelet", "bracelate", "bangle", "kada")):
        product_type = "a fashion bracelet"
    elif any(word in lowered for word in ("necklace", "pendant", "chain")):
        product_type = "a fashion necklace"
    elif "watch" in lowered:
        product_type = "a fashion watch"
    else:
        product_type = "a jewellery piece"

    if any(word in lowered for word in ("flower", "floral", "tulip", "sunflower")):
        detail = "floral-inspired detailing"
    elif any(word in lowered for word in ("peacock", "mayil")):
        detail = "peacock-inspired detailing"
    elif "leaf" in lowered:
        detail = "botanical leaf detailing"
    elif "heart" in lowered:
        detail = "a romantic heart motif"
    elif any(word in lowered for word in ("moon", "star", "galaxy", "celestial")):
        detail = "celestial-inspired detailing"
    elif "pearl" in lowered:
        detail = "pearl-inspired accents"
    elif any(word in lowered for word in ("crystal", "rhinestone")):
        detail = "crystal-inspired accents"
    elif any(word in lowered for word in ("oxidized", "silver")):
        detail = "silver-tone detailing"
    elif "gold" in lowered:
        detail = "gold-tone detailing"
    else:
        detail = "distinctive detailing reflected in the listing images"

    return (
        f"{name} is {product_type} with {detail}, designed to add a polished "
        "accent to everyday and occasion outfits. Its versatile styling makes "
        "it a thoughtful jewellery gift for birthdays, anniversaries and "
        "personal celebrations. Refer to the listing images for the exact "
        "colour, finish, scale and proportions."
    )


def _description_for(
    row: CatalogueRow,
    *,
    existing: str | None = None,
) -> str:
    source = sanitize_html(row.description)
    if _has_substantive_description(source, name=row.name):
        return source or _generated_description(row)
    if _has_substantive_description(existing, name=row.name):
        return existing or _generated_description(row)
    return _generated_description(row)


def _is_wishkr_hosted_image(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").casefold()
    return hostname == "wishkr.com" or hostname.endswith(".wishkr.com")


def _images_for(
    product: Product,
    workbook_images: list[str],
    *,
    created: bool,
) -> tuple[list[str], str | None]:
    """Keep durable Wishkr uploads for matches; workbook images seed new items."""
    existing_images = _unique(
        [*(product.product_images or []), product.image_url or ""]
    )
    if not created and any(
        _is_wishkr_hosted_image(image) for image in existing_images
    ):
        gallery = list(product.product_images or [])
        if not gallery and product.image_url:
            gallery = [product.image_url]
        return gallery, product.image_url or (gallery[0] if gallery else None)
    return workbook_images, workbook_images[0] if workbook_images else None


def occasions_for(row: CatalogueRow) -> list[str]:
    """Reviewed occasion mapping for jewellery, ordered by shopper relevance."""
    name = row.name.casefold()
    product_category = (row.product_category or "").casefold()

    if "heart" in name:
        return [
            "birthday",
            "anniversary",
            "proposal",
            "romantic-surprise",
            "valentines-day",
            "just-because",
        ]
    if any(word in name for word in ("flower", "floral", "rose", "tulip", "sunflower", "daisy")):
        return [
            "birthday",
            "anniversary",
            "romantic-surprise",
            "valentines-day",
            "mothers-day",
            "just-because",
        ]
    if "traditional" in product_category or any(
        word in name
        for word in ("jhum", "chandbali", "oxidized", "temple", "dangler", "doli")
    ):
        return [
            "birthday",
            "anniversary",
            "wedding",
            "engagement",
            "diwali",
            "just-because",
        ]
    if any(word in product_category for word in ("kada", "bracelet")) or any(
        word in name for word in ("bracelet", "bracelate", "bangle", "kada", "watch")
    ):
        occasions = ["birthday", "anniversary", "rakhi", "just-because"]
        if "kada" in name:
            occasions[2:2] = ["wedding", "engagement", "diwali"]
        return occasions
    if "horseshoe" in name:
        return ["birthday", "anniversary", "congratulations", "just-because"]
    return ["birthday", "anniversary", "just-because"]


def _variant_name(row: CatalogueRow) -> str:
    generic_sizes = {"free", "free size", "one size", "onesize"}
    details = _unique(
        value
        for value in (row.colour_name, row.size, row.custom_sku)
        if value and value.strip().casefold() not in generic_sizes
    )
    return " / ".join(details) or row.sku_id


def _record_change(
    summary: SyncSummary,
    target: Any,
    field_name: str,
    desired: Any,
    *,
    label: str,
) -> bool:
    current = getattr(target, field_name, None)
    if current == desired:
        return False
    setattr(target, field_name, desired)
    summary.changed(field_name)
    summary.changes.append(f"{label}: {field_name} {current!r} -> {desired!r}")
    return True


def _find_group_product(
    group: SourceGroup,
    *,
    products_by_id: dict[str, Product],
    products_by_name: dict[str, list[Product]],
    products_by_image: dict[str, list[Product]],
    products_by_variant_sku: dict[str, list[Product]],
    unique_source_names: set[str],
) -> tuple[Product | None, str]:
    candidates: dict[str, Product] = {}
    methods: set[str] = set()
    for item in group.items:
        row = item.catalogue
        manual_id = MANUAL_MATCH_OVERRIDES.get(row.sku_id.casefold())
        if manual_id:
            product = products_by_id.get(manual_id)
            if product is None:
                raise RuntimeError(f"manual match target {manual_id} is absent")
            candidates[product.id] = product
            methods.add("manual override")
            continue

        derived = products_by_id.get(product_id_for_sku(row.sku_id))
        if derived is not None:
            candidates[derived.id] = derived
            methods.add("derived SKU ID")
            continue

        variant_matches = products_by_variant_sku.get(variant_sku(row.sku_id).casefold(), [])
        if len(variant_matches) == 1:
            candidates[variant_matches[0].id] = variant_matches[0]
            methods.add("variant SKU")
            continue

        image_matches: dict[str, Product] = {}
        for image in row.images:
            for product in products_by_image.get(_image_key(image), []):
                image_matches[product.id] = product
        if len(image_matches) == 1:
            product = next(iter(image_matches.values()))
            candidates[product.id] = product
            methods.add("image URL")
            continue

        name_key = _name_key(row.name)
        name_matches = products_by_name.get(name_key, [])
        if name_key in unique_source_names and len(name_matches) == 1:
            candidates[name_matches[0].id] = name_matches[0]
            methods.add("normalised name")

    if len(candidates) > 1:
        detail = ", ".join(f"{product.name} [{product.id}]" for product in candidates.values())
        raise RuntimeError(
            f"ambiguous match for workbook product {group.primary.catalogue.name!r}: {detail}"
        )
    if not candidates:
        return None, "new product"
    return next(iter(candidates.values())), ", ".join(sorted(methods))


async def _sync_variants(
    db: AsyncSession,
    product: Product,
    group: SourceGroup,
    summary: SyncSummary,
) -> bool:
    changed = False
    desired_ids: set[str] = set()
    existing_by_sku = {
        variant.sku.casefold(): variant
        for variant in product.variants
        if variant.sku
    }
    for sort_order, item in enumerate(group.items, start=1):
        row = item.catalogue
        sku = variant_sku(row.sku_id)
        desired_id = variant_id_for_sku(row.sku_id)
        variant = existing_by_sku.get(sku.casefold())
        if variant is None:
            variant = next(
                (
                    existing
                    for existing in product.variants
                    if existing.id == desired_id
                ),
                None,
            )
        if variant is None:
            variant = ProductVariant(
                id=desired_id,
                product_id=product.id,
                name=_variant_name(row),
                sku=sku,
                price_delta_paise=row.mrp_paise,
                discounted_price_paise=row.selling_paise,
                inventory_quantity=item.inventory.quantity,
                active=True,
                sort_order=sort_order,
            )
            db.add(variant)
            product.variants.append(variant)
            summary.variants_created += 1
            summary.changes.append(
                f"{product.name}: created variant {_variant_name(row)!r} [{sku}]"
            )
            changed = True
        else:
            variant_changed = False
            label = f"{product.name} / {_variant_name(row)}"
            variant_changed |= _record_change(
                summary, variant, "name", _variant_name(row), label=label
            )
            # An existing SKU can be referenced by orders and integrations.
            # Workbook UUIDs seed new variant SKUs but never rewrite old ones.
            variant_changed |= _record_change(
                summary,
                variant,
                "price_delta_paise",
                row.mrp_paise,
                label=label,
            )
            variant_changed |= _record_change(
                summary,
                variant,
                "discounted_price_paise",
                row.selling_paise,
                label=label,
            )
            variant_changed |= _record_change(
                summary,
                variant,
                "inventory_quantity",
                item.inventory.quantity,
                label=label,
            )
            variant_changed |= _record_change(summary, variant, "active", True, label=label)
            variant_changed |= _record_change(
                summary, variant, "sort_order", sort_order, label=label
            )
            if variant_changed:
                summary.variants_updated += 1
                changed = True
        desired_ids.add(variant.id)

    for variant in product.variants:
        if variant.id not in desired_ids and variant.active:
            variant.active = False
            summary.variants_deactivated += 1
            summary.changes.append(
                f"{product.name}: deactivated variant {variant.name!r} [{variant.id}]"
            )
            changed = True
    return changed


async def sync_catalogue(
    db: AsyncSession,
    groups: list[SourceGroup],
    *,
    vendor_name: str = DEFAULT_VENDOR_NAME,
) -> SyncSummary:
    vendors = (await db.execute(select(Vendor))).scalars().all()
    vendor_matches = [
        vendor for vendor in vendors if vendor.name.strip().casefold() == vendor_name.casefold()
    ]
    if len(vendor_matches) != 1:
        raise RuntimeError(
            f"expected exactly one vendor named {vendor_name!r}; found {len(vendor_matches)}"
        )
    vendor = vendor_matches[0]

    products = (
        (
            await db.execute(
                select(Product)
                .where(Product.vendor_id == vendor.id)
                .options(selectinload(Product.variants), selectinload(Product.addons))
            )
        )
        .scalars()
        .unique()
        .all()
    )
    products_by_id = {product.id: product for product in products}
    products_by_name: dict[str, list[Product]] = {}
    products_by_image: dict[str, list[Product]] = {}
    products_by_variant_sku: dict[str, list[Product]] = {}
    for product in products:
        products_by_name.setdefault(_name_key(product.name), []).append(product)
        for image in _unique([product.image_url or "", *(product.product_images or [])]):
            products_by_image.setdefault(_image_key(image), []).append(product)
        for variant in product.variants:
            if variant.sku:
                products_by_variant_sku.setdefault(variant.sku.casefold(), []).append(product)

    source_name_counts: dict[str, int] = {}
    for group in groups:
        key = _name_key(group.primary.catalogue.name)
        source_name_counts[key] = source_name_counts.get(key, 0) + 1
    unique_source_names = {
        key for key, count in source_name_counts.items() if count == 1
    }

    summary = SyncSummary(
        source_skus=sum(len(group.items) for group in groups),
        source_products=len(groups),
    )
    matched_existing_ids: set[str] = set()
    for group in groups:
        source = group.primary.catalogue
        product, method = _find_group_product(
            group,
            products_by_id=products_by_id,
            products_by_name=products_by_name,
            products_by_image=products_by_image,
            products_by_variant_sku=products_by_variant_sku,
            unique_source_names=unique_source_names,
        )
        created = product is None
        if product is None:
            product_id = product_id_for_sku(source.sku_id)
            collision = await db.get(Product, product_id)
            if collision is not None:
                raise RuntimeError(
                    f"derived product ID {product_id} already belongs to {collision.name!r}"
                )
            product = Product(
                id=product_id,
                vendor_id=vendor.id,
                name=source.name,
                description=_description_for(source),
                category="Jewellery",
                occasions=occasions_for(source),
                preparation_time=None,
                dispatch_lead_time_days=None,
                delivery_pincodes=[],
                ships_nationally=True,
                is_bestseller=source.is_bestseller,
                is_customisable=False,
                supports_gift_message=False,
                product_images=[],
                care_instructions=None,
                whats_included=None,
                customization_schema=[],
                base_price_paise=source.mrp_paise,
                discounted_price_paise=source.selling_paise,
                inventory_quantity=group.inventory_quantity,
                gst_rate=None,
                hsn=source.hsn,
                shipping_included=True,
                shipping_fee_paise=None,
                image_url=None,
                video_url=None,
                active=True,
                in_stock=(
                    group.inventory_quantity is None or group.inventory_quantity > 0
                ),
                lifecycle_status="active",
                variants=[],
                addons=[],
            )
            db.add(product)
            await db.flush()
            summary.products_created += 1
            summary.changes.append(
                f"{source.name}: created product [{product.id}] from {source.sku_id}"
            )
            products.append(product)
            products_by_id[product.id] = product
        else:
            matched_existing_ids.add(product.id)

        workbook_images = _unique(
            image for item in group.items for image in item.catalogue.images
        )
        images, primary_image = _images_for(
            product,
            workbook_images,
            created=created,
        )
        product_changed = False
        label = f"{source.name} [{product.id}]"
        desired_fields = {
            "name": source.name,
            "description": _description_for(source, existing=product.description),
            "category": "Jewellery",
            "occasions": occasions_for(source),
            "is_bestseller": source.is_bestseller,
            "product_images": images,
            "base_price_paise": source.mrp_paise,
            "discounted_price_paise": source.selling_paise,
            "inventory_quantity": group.inventory_quantity,
            "hsn": source.hsn,
            "image_url": primary_image,
            "in_stock": (
                group.inventory_quantity is None or group.inventory_quantity > 0
            ),
        }
        for field_name, desired in desired_fields.items():
            product_changed |= _record_change(
                summary, product, field_name, desired, label=label
            )

        if group.has_variants:
            product_changed |= await _sync_variants(db, product, group, summary)
        else:
            for variant in product.variants:
                if variant.active:
                    variant.active = False
                    summary.variants_deactivated += 1
                    summary.changes.append(
                        f"{product.name}: deactivated non-workbook variant "
                        f"{variant.name!r} [{variant.id}]"
                    )
                    product_changed = True
        if product_changed and not created:
            summary.products_updated += 1
        summary.changes.append(f"{label}: matched via {method}")

    source_product_ids = matched_existing_ids | {
        product_id_for_sku(group.primary.catalogue.sku_id)
        for group in groups
        if product_id_for_sku(group.primary.catalogue.sku_id) in products_by_id
    }
    summary.missing_from_workbook = sorted(
        [
            (product.id, product.name)
            for product in products
            if product.id not in source_product_ids
        ],
        key=lambda item: (item[1].casefold(), item[0]),
    )
    return summary


def print_summary(summary: SyncSummary, *, committed: bool) -> None:
    print("PYAARISAKHEE_SYNC_BEGIN")
    print(f"mode={'COMMIT' if committed else 'DRY RUN (ROLLBACK)'}")
    print(f"source_skus={summary.source_skus}")
    print(f"source_products={summary.source_products}")
    print(f"products_created={summary.products_created}")
    print(f"products_updated={summary.products_updated}")
    print(f"variants_created={summary.variants_created}")
    print(f"variants_updated={summary.variants_updated}")
    print(f"variants_deactivated={summary.variants_deactivated}")
    print(f"total_changes={summary.total_changes}")
    for field_name, count in sorted(summary.field_changes.items()):
        print(f"field_changes.{field_name}={count}")
    print("changes:")
    for change in summary.changes:
        print(f"  - {change}")
    print("database_products_missing_from_workbook (review only; never deleted):")
    if summary.missing_from_workbook:
        for product_id, name in summary.missing_from_workbook:
            print(f"  - {name} [{product_id}]")
    else:
        print("  - none")
    print("PYAARISAKHEE_SYNC_END")


async def run(
    workbook: Path,
    *,
    commit: bool,
    vendor_name: str,
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    invalidate_cache: bool = True,
) -> SyncSummary:
    groups = load_workbook(workbook)
    async with session_factory() as db:
        try:
            summary = await sync_catalogue(db, groups, vendor_name=vendor_name)
            if commit:
                await db.commit()
            else:
                await db.rollback()
        except Exception:
            await db.rollback()
            raise

    if commit and invalidate_cache:
        await invalidate_product_cache()
    print_summary(summary, committed=commit)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path(__file__).with_name("PyaariSakhee.xlsx"),
        help="path to the audited PyaariSakhee.xlsx file",
    )
    parser.add_argument("--vendor-name", default=DEFAULT_VENDOR_NAME)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="persist the validated sync; without this flag the transaction is rolled back",
    )
    args = parser.parse_args()
    asyncio.run(
        run(
            args.workbook.resolve(),
            commit=args.commit,
            vendor_name=args.vendor_name,
        )
    )


if __name__ == "__main__":
    main()
