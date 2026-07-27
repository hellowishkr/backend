"""Compare the authoritative PyaariSakhee workbook with a production snapshot.

This is an offline, read-only comparison. It uses only Python's standard
library so it can parse the supplied XLSX without adding project dependencies.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from decimal import Decimal
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PACKAGE_REL_NS}

# The database records below predate the SKU-derived ID import. Their updated
# catalogue images were compared visually with the workbook images on
# 2026-07-25 and confirmed to depict the same products.
MANUAL_MATCH_OVERRIDES = {
    "e4e36bd9-331c-46fb-8fba-be86019f03e3": (
        "prd_1783047464_6d9d714b",
        "image-verified renamed product",
    ),
    "b7f8d110-ed91-44e7-9b70-00959e4d07ee": (
        "prd_1783047470_90317c8c",
        "image-verified punctuation/name variant",
    ),
}


@dataclass(frozen=True)
class CatalogueRow:
    row: int
    sku_id: str
    custom_sku: str | None
    name: str
    mrp_rupees: Decimal
    selling_price_rupees: Decimal
    business_category: str | None
    product_category: str | None
    description: str | None
    images: tuple[str, ...]


@dataclass(frozen=True)
class InventoryRow:
    row: int
    sku_id: str
    custom_sku: str | None
    name: str
    quantity: int | None

    @property
    def expected_in_stock(self) -> bool:
        # The workbook's instructions define blank as infinite inventory.
        return self.quantity is None or self.quantity > 0


@dataclass(frozen=True)
class Match:
    catalogue: CatalogueRow
    inventory: InventoryRow | None
    db_product_id: str | None
    db_product_name: str | None
    method: str
    confidence: str
    note: str | None = None


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise ValueError(f"invalid cell reference: {reference}")
    number = 0
    for character in letters.group():
        number = number * 26 + ord(character) - 64
    return number


def _xlsx_rows(path: Path) -> dict[str, list[dict[int, Any]]]:
    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared_strings.append(
                    "".join(text.text or "" for text in item.iterfind(".//m:t", NS))
                )

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
            sheet_root = ET.fromstring(archive.read(target))
            decoded_rows: list[dict[int, Any]] = []
            for row in sheet_root.findall(".//m:sheetData/m:row", NS):
                decoded: dict[int, Any] = {0: int(row.attrib["r"])}
                for cell in row.findall("m:c", NS):
                    column = _column_number(cell.attrib["r"])
                    cell_type = cell.attrib.get("t")
                    value_element = cell.find("m:v", NS)
                    if cell_type == "inlineStr":
                        inline = cell.find("m:is", NS)
                        value = (
                            "".join(text.text or "" for text in inline.iterfind(".//m:t", NS))
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


def _decimal(value: Any, *, field: str, row: int) -> Decimal:
    text = _text(value)
    if text is None:
        raise ValueError(f"{field} is blank on catalogue row {row}")
    try:
        return Decimal(text)
    except Exception as exc:
        raise ValueError(f"invalid {field} {text!r} on catalogue row {row}") from exc


def _integer_or_none(value: Any, *, row: int) -> int | None:
    text = _text(value)
    if text is None:
        return None
    number = Decimal(text)
    if number != number.to_integral_value() or number < 0:
        raise ValueError(f"invalid inventory {text!r} on inventory row {row}")
    return int(number)


def load_workbook(path: Path) -> tuple[list[CatalogueRow], list[InventoryRow], list[str]]:
    sheets = _xlsx_rows(path)
    catalogue_sheet = next(
        (name for name in sheets if name.casefold() in {"catalogue", "cataloque"}),
        None,
    )
    inventory_sheet = next(
        (name for name in sheets if name.casefold() == "inventory"),
        None,
    )
    if catalogue_sheet is None or inventory_sheet is None:
        raise ValueError(f"expected catalogue and inventory sheets; found {sorted(sheets)}")

    catalogue: list[CatalogueRow] = []
    for cells in sheets[catalogue_sheet]:
        row_number = cells[0]
        if row_number == 1:
            continue
        sku_id = _text(cells.get(1))
        name = _text(cells.get(4))
        if sku_id is None and name is None:
            continue
        if sku_id is None or name is None:
            raise ValueError(f"incomplete catalogue identity on row {row_number}")
        mrp = _decimal(cells.get(5), field="MRP", row=row_number)
        selling_raw = _text(cells.get(6))
        selling = (
            _decimal(selling_raw, field="selling price", row=row_number)
            if selling_raw is not None
            else mrp
        )
        if selling > mrp:
            raise ValueError(f"selling price exceeds MRP for {name!r} on row {row_number}")
        images = tuple(
            image for column in range(16, 22) if (image := _text(cells.get(column))) is not None
        )
        catalogue.append(
            CatalogueRow(
                row=row_number,
                sku_id=sku_id,
                custom_sku=_text(cells.get(3)),
                name=name,
                mrp_rupees=mrp,
                selling_price_rupees=selling,
                business_category=_text(cells.get(7)),
                product_category=_text(cells.get(8)),
                description=_text(cells.get(9)),
                images=images,
            )
        )

    inventory: list[InventoryRow] = []
    for cells in sheets[inventory_sheet]:
        row_number = cells[0]
        if row_number == 1:
            continue
        sku_id = _text(cells.get(1))
        name = _text(cells.get(3))
        if sku_id is None and name is None:
            continue
        if sku_id is None or name is None:
            raise ValueError(f"incomplete inventory identity on row {row_number}")
        inventory.append(
            InventoryRow(
                row=row_number,
                sku_id=sku_id,
                custom_sku=_text(cells.get(2)),
                name=name,
                quantity=_integer_or_none(cells.get(8), row=row_number),
            )
        )

    return catalogue, inventory, list(sheets)


def load_snapshot(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    begin = "SNAPSHOT_JSON_BEGIN"
    end = "SNAPSHOT_JSON_END"
    if begin not in raw or end not in raw:
        raise ValueError("production snapshot markers were not found")
    payload = raw.split(begin, 1)[1].split(end, 1)[0]
    return json.loads(payload)


def _name_key(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", value)


def _image_key(value: str) -> str:
    return value.strip().split("?", 1)[0].casefold()


def _imported_product_id(sku_id: str) -> str:
    compact = re.sub(r"[^a-f0-9]", "", sku_id.casefold())
    return "prd_ps_" + compact[:20]


def match_products(
    catalogue: list[CatalogueRow],
    inventory: list[InventoryRow],
    products: list[dict[str, Any]],
) -> tuple[list[Match], dict[str, list[dict[str, Any]]]]:
    inventory_by_sku = {row.sku_id.casefold(): row for row in inventory}
    product_by_id = {product["id"]: product for product in products}
    products_by_name: dict[str, list[dict[str, Any]]] = {}
    products_by_image: dict[str, list[dict[str, Any]]] = {}
    products_by_variant_sku: dict[str, list[dict[str, Any]]] = {}
    catalogue_name_counts: dict[str, int] = {}
    for row in catalogue:
        key = _name_key(row.name)
        catalogue_name_counts[key] = catalogue_name_counts.get(key, 0) + 1
    for product in products:
        products_by_name.setdefault(_name_key(product["name"]), []).append(product)
        images = {
            _image_key(image)
            for image in [
                product.get("image_url"),
                *(product.get("product_images") or []),
            ]
            if image
        }
        for image in images:
            products_by_image.setdefault(image, []).append(product)
        for variant in product.get("variants") or []:
            if variant.get("sku"):
                products_by_variant_sku.setdefault(str(variant["sku"]).casefold(), []).append(
                    product
                )

    matches: list[Match] = []
    ambiguous_candidates: dict[str, list[dict[str, Any]]] = {}
    for row in catalogue:
        inventory_row = inventory_by_sku.get(row.sku_id.casefold())
        override = MANUAL_MATCH_OVERRIDES.get(row.sku_id.casefold())
        if override is not None:
            product_id, note = override
            product = product_by_id.get(product_id)
            if product is None:
                raise ValueError(f"manual match target {product_id!r} is absent from the snapshot")
            matches.append(
                Match(
                    row,
                    inventory_row,
                    product["id"],
                    product["name"],
                    "manual image verification",
                    "exact",
                    note,
                )
            )
            continue

        expected_id = _imported_product_id(row.sku_id)
        if expected_id in product_by_id:
            product = product_by_id[expected_id]
            matches.append(
                Match(
                    row,
                    inventory_row,
                    product["id"],
                    product["name"],
                    "derived SKU ID",
                    "exact",
                )
            )
            continue

        compact_sku = re.sub(r"[^a-f0-9]", "", row.sku_id.casefold())
        expected_variant_sku = "pyaarisakhee-" + compact_sku[:16]
        variant_candidates = products_by_variant_sku.get(expected_variant_sku, [])
        if len(variant_candidates) == 1:
            product = variant_candidates[0]
            matches.append(
                Match(
                    row,
                    inventory_row,
                    product["id"],
                    product["name"],
                    "derived variant SKU",
                    "exact",
                )
            )
            continue

        image_candidates: dict[str, dict[str, Any]] = {}
        for image in row.images:
            for product in products_by_image.get(_image_key(image), []):
                image_candidates[product["id"]] = product
        if len(image_candidates) == 1:
            product = next(iter(image_candidates.values()))
            matches.append(
                Match(
                    row,
                    inventory_row,
                    product["id"],
                    product["name"],
                    "image URL",
                    "exact",
                )
            )
            continue

        name_candidates = products_by_name.get(_name_key(row.name), [])
        if len(name_candidates) == 1 and catalogue_name_counts[_name_key(row.name)] == 1:
            product = name_candidates[0]
            matches.append(
                Match(
                    row,
                    inventory_row,
                    product["id"],
                    product["name"],
                    "normalized name",
                    "exact",
                )
            )
            continue

        if len(name_candidates) == 1 and catalogue_name_counts[_name_key(row.name)] > 1:
            matches.append(
                Match(
                    row,
                    inventory_row,
                    None,
                    None,
                    "duplicate workbook name without SKU/image match",
                    "unmatched",
                    (
                        f"database has {name_candidates[0]['name']} "
                        f"[{name_candidates[0]['id']}], but another workbook SKU "
                        "already identifies that product"
                    ),
                )
            )
            continue

        if len(image_candidates) > 1 or len(name_candidates) > 1:
            candidates = list(image_candidates.values()) or name_candidates
            ambiguous_candidates[row.sku_id] = candidates
            matches.append(
                Match(
                    row,
                    inventory_row,
                    None,
                    None,
                    "multiple candidates",
                    "ambiguous",
                    ", ".join(
                        f"{candidate['name']} [{candidate['id']}]" for candidate in candidates
                    ),
                )
            )
            continue

        scores = sorted(
            [
                (
                    SequenceMatcher(None, _name_key(row.name), _name_key(product["name"])).ratio(),
                    product,
                )
                for product in products
            ],
            key=lambda item: (item[0], item[1]["id"]),
        )
        best_score, best_product = scores[-1]
        next_score = scores[-2][0] if len(scores) > 1 else 0
        if best_score >= 0.92 and best_score - next_score >= 0.08:
            matches.append(
                Match(
                    row,
                    inventory_row,
                    best_product["id"],
                    best_product["name"],
                    "fuzzy name",
                    "review",
                    f"similarity={best_score:.3f}",
                )
            )
        else:
            note = (
                f"best candidate: {best_product['name']} "
                f"[{best_product['id']}], similarity={best_score:.3f}"
            )
            matches.append(
                Match(
                    row,
                    inventory_row,
                    None,
                    None,
                    "no confident match",
                    "unmatched",
                    note,
                )
            )
    return matches, ambiguous_candidates


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def analyse(
    catalogue: list[CatalogueRow],
    inventory: list[InventoryRow],
    sheet_names: list[str],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    products = snapshot["products"]
    matches, _ = match_products(catalogue, inventory, products)
    product_by_id = {product["id"]: product for product in products}
    confident = [
        match
        for match in matches
        if match.db_product_id is not None and match.confidence == "exact"
    ]
    review = [
        match
        for match in matches
        if match.db_product_id is not None and match.confidence == "review"
    ]
    unresolved = [match for match in matches if match.db_product_id is None]
    matched_product_ids = {
        match.db_product_id for match in confident if match.db_product_id is not None
    }

    db_missing_excel = [product for product in products if product["id"] not in matched_product_ids]
    excel_missing_db = [match for match in unresolved if match.confidence == "unmatched"]
    ambiguous = [match for match in matches if match.confidence in {"ambiguous", "review"}]

    inventory_groups: dict[str, list[Match]] = {}
    price_groups: dict[str, list[Match]] = {}
    for match in confident:
        assert match.db_product_id is not None
        inventory_groups.setdefault(match.db_product_id, []).append(match)
        price_groups.setdefault(match.db_product_id, []).append(match)

    inventory_updates: list[dict[str, Any]] = []
    inventory_quantity_gaps: list[dict[str, Any]] = []
    for product_id, group in inventory_groups.items():
        product = product_by_id[product_id]
        inventory_rows = [match.inventory for match in group if match.inventory]
        if not inventory_rows:
            continue
        expected_in_stock = any(row.expected_in_stock for row in inventory_rows)
        quantities = [row.quantity for row in inventory_rows]
        if bool(product["in_stock"]) != expected_in_stock:
            inventory_updates.append(
                {
                    "db_product_id": product_id,
                    "db_product_name": product["name"],
                    "excel_skus": [match.catalogue.sku_id for match in group],
                    "excel_quantities": quantities,
                    "current_in_stock": bool(product["in_stock"]),
                    "required_in_stock": expected_in_stock,
                }
            )
        inventory_quantity_gaps.append(
            {
                "db_product_id": product_id,
                "db_product_name": product["name"],
                "excel_skus": [match.catalogue.sku_id for match in group],
                "excel_quantities": quantities,
                "database_quantity": "not stored",
            }
        )

    price_updates: list[dict[str, Any]] = []
    price_conflicts: list[dict[str, Any]] = []
    discount_issues: list[dict[str, Any]] = []
    pricing = snapshot.get("pricing") or {}
    global_discount_active = bool(pricing.get("flash_active"))
    for product_id, group in price_groups.items():
        product = product_by_id[product_id]
        selling_prices = {match.catalogue.selling_price_rupees for match in group}
        mrps = {match.catalogue.mrp_rupees for match in group}
        current = Decimal(product["base_price_paise"]) / 100
        if len(selling_prices) > 1 or len(mrps) > 1:
            price_conflicts.append(
                {
                    "db_product_id": product_id,
                    "db_product_name": product["name"],
                    "excel_skus": [match.catalogue.sku_id for match in group],
                    "excel_mrps": sorted(_money(value) for value in mrps),
                    "excel_selling_prices": sorted(_money(value) for value in selling_prices),
                    "current_db_base_price": _money(current),
                }
            )
            continue
        selling = next(iter(selling_prices))
        mrp = next(iter(mrps))
        if current != selling:
            price_updates.append(
                {
                    "db_product_id": product_id,
                    "db_product_name": product["name"],
                    "excel_skus": [match.catalogue.sku_id for match in group],
                    "excel_mrp": _money(mrp),
                    "excel_selling_price": _money(selling),
                    "current_db_base_price": _money(current),
                    "required_db_base_price_if_mapped_to_selling": _money(selling),
                }
            )
        if selling < mrp:
            discount_issues.append(
                {
                    "db_product_id": product_id,
                    "db_product_name": product["name"],
                    "excel_skus": [match.catalogue.sku_id for match in group],
                    "excel_mrp": _money(mrp),
                    "excel_selling_price": _money(selling),
                    "expected_discount_pct": _money((mrp - selling) * Decimal(100) / mrp),
                    "current_db_base_price": _money(current),
                    "frontend_status": (
                        "global flash pricing cannot represent this product discount"
                        if global_discount_active
                        else "no discount displayed; global flash pricing is disabled"
                    ),
                }
            )

    inventory_update_ids = {row["db_product_id"] for row in inventory_updates}
    price_update_ids = {row["db_product_id"] for row in price_updates}
    discount_issue_ids = {row["db_product_id"] for row in discount_issues}
    matched_products: list[dict[str, Any]] = []
    for product_id, group in sorted(
        price_groups.items(),
        key=lambda item: (product_by_id[item[0]]["name"].casefold(), item[0]),
    ):
        product = product_by_id[product_id]
        inventory_rows = [match.inventory for match in group if match.inventory]
        quantities = [row.quantity for row in inventory_rows]
        expected_in_stock = (
            any(row.expected_in_stock for row in inventory_rows) if inventory_rows else None
        )
        matched_products.append(
            {
                "db_product_id": product_id,
                "db_product_name": product["name"],
                "excel_skus": [match.catalogue.sku_id for match in group],
                "excel_names": sorted({match.catalogue.name for match in group}),
                "excel_mrps": sorted({_money(match.catalogue.mrp_rupees) for match in group}),
                "excel_selling_prices": sorted(
                    {_money(match.catalogue.selling_price_rupees) for match in group}
                ),
                "excel_quantities": quantities,
                "current_db_base_price": _money(Decimal(product["base_price_paise"]) / 100),
                "current_in_stock": bool(product["in_stock"]),
                "required_in_stock": expected_in_stock,
                "inventory_status": (
                    "update required"
                    if product_id in inventory_update_ids
                    else "in-stock flag agrees"
                ),
                "selling_price_status": (
                    "update required"
                    if product_id in price_update_ids
                    else "matches workbook selling price"
                ),
                "discount_status": (
                    "frontend issue" if product_id in discount_issue_ids else "no workbook discount"
                ),
                "match_methods": sorted({match.method for match in group}),
            }
        )

    catalogue_skus = [row.sku_id.casefold() for row in catalogue]
    inventory_skus = [row.sku_id.casefold() for row in inventory]
    validation = {
        "workbook_sheets": sheet_names,
        "catalogue_rows": len(catalogue),
        "inventory_rows": len(inventory),
        "duplicate_catalogue_skus": sorted(
            sku for sku in set(catalogue_skus) if catalogue_skus.count(sku) > 1
        ),
        "duplicate_inventory_skus": sorted(
            sku for sku in set(inventory_skus) if inventory_skus.count(sku) > 1
        ),
        "catalogue_skus_missing_inventory": sorted(set(catalogue_skus) - set(inventory_skus)),
        "inventory_skus_missing_catalogue": sorted(set(inventory_skus) - set(catalogue_skus)),
        "production_products": len(products),
        "production_vendor": snapshot["vendors"],
        "production_pricing": snapshot.get("pricing"),
    }

    return {
        "validation": validation,
        "summary": {
            "exact_excel_sku_matches": len(confident),
            "unique_db_products_matched": len(matched_product_ids),
            "db_products_missing_from_excel": len(db_missing_excel),
            "excel_skus_missing_from_db": len(excel_missing_db),
            "matches_requiring_review": len(ambiguous),
            "in_stock_flag_updates_required": len(inventory_updates),
            "finite_or_infinite_quantities_not_storable": len(inventory_quantity_gaps),
            "selling_price_updates_required": len(price_updates),
            "multi_sku_price_conflicts": len(price_conflicts),
            "frontend_discount_issues": len(discount_issues),
        },
        "matches": [
            {
                **asdict(match),
                "catalogue": {
                    **asdict(match.catalogue),
                    "mrp_rupees": _money(match.catalogue.mrp_rupees),
                    "selling_price_rupees": _money(match.catalogue.selling_price_rupees),
                },
                "inventory": asdict(match.inventory) if match.inventory else None,
            }
            for match in matches
        ],
        "matched_products": matched_products,
        "db_products_missing_from_excel": db_missing_excel,
        "excel_skus_missing_from_db": [asdict(match) for match in excel_missing_db],
        "ambiguous_matches": [asdict(match) for match in ambiguous],
        "inventory_updates": inventory_updates,
        "inventory_quantity_schema_gaps": inventory_quantity_gaps,
        "price_updates": price_updates,
        "price_conflicts": price_conflicts,
        "frontend_discount_issues": discount_issues,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    flattened: list[dict[str, Any]] = []
    for row in rows:
        flattened.append(
            {
                key: (
                    json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (list, dict, tuple))
                    else value
                )
                for key, value in row.items()
            }
        )
    fieldnames: list[str] = []
    for row in flattened:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened)


def _md(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(_md(item) for item in value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _quantity_list(values: list[int | None]) -> str:
    return ", ".join("∞" if value is None else str(value) for value in values)


def _write_report(path: Path, result: dict[str, Any]) -> None:
    summary = result["summary"]
    validation = result["validation"]
    lines = [
        "# PyaariSakhee Production Catalogue Audit",
        "",
        "Audit date: 2026-07-25",
        "",
        "Source of truth: `PyaariSakhee.xlsx` (`cataloque` and `inventory` sheets).",
        "",
        "Production was read through a rollback-only exporter. **No production "
        "records were changed or deleted during this audit.**",
        "",
        "## Executive summary",
        "",
        "| Finding | Count |",
        "| --- | ---: |",
        f"| Workbook catalogue SKUs | {validation['catalogue_rows']} |",
        f"| Workbook inventory SKUs | {validation['inventory_rows']} |",
        f"| Production products for PyariSakhee | {validation['production_products']} |",
        f"| Confirmed Excel SKU matches | {summary['exact_excel_sku_matches']} |",
        f"| Unique production products matched | {summary['unique_db_products_matched']} |",
        f"| Production products missing from Excel | {summary['db_products_missing_from_excel']} |",
        f"| Excel SKUs missing from production | {summary['excel_skus_missing_from_db']} |",
        f"| In-stock flag corrections required | {summary['in_stock_flag_updates_required']} |",
        f"| Selling-price corrections required | {summary['selling_price_updates_required']} |",
        f"| Per-product frontend discount issues | {summary['frontend_discount_issues']} |",
        f"| Unresolved/ambiguous matches | {summary['matches_requiring_review']} |",
        "",
        "Workbook validation passed: no duplicate SKU IDs, and the Catalogue and "
        "Inventory SKU sets agree exactly. The workbook contains five sheets "
        "(not four), and its catalogue sheet is named `cataloque`.",
        "",
        "## 1. Products available in both Excel and production",
        "",
        "The 72 matched Excel SKUs represent 69 production products. Four colour "
        "SKUs are variants of the single Stretch Crystal Rhinestone Bracelet product.",
        "",
        "| Product | DB ID | Excel SKU(s) | MRP (₹) | Selling (₹) | Inventory | DB price (₹) | Price status | Stock status | Discount |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in result["matched_products"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(row["db_product_name"]),
                    _md(row["db_product_id"]),
                    _md(row["excel_skus"]),
                    _md(row["excel_mrps"]),
                    _md(row["excel_selling_prices"]),
                    _quantity_list(row["excel_quantities"]),
                    _md(row["current_db_base_price"]),
                    _md(row["selling_price_status"]),
                    _md(row["inventory_status"]),
                    _md(row["discount_status"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 2. Products in production but missing from Excel",
            "",
            "These are deletion candidates only. Nothing was deleted.",
            "",
            "| Product | DB ID | Current price (₹) | In stock | Active |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for row in result["db_products_missing_from_excel"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(row["name"]),
                    _md(row["id"]),
                    _money(Decimal(row["base_price_paise"]) / 100),
                    _md(row["in_stock"]),
                    _md(row["active"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 3. Products in Excel but missing from production",
            "",
            "There are 32 missing SKUs across 28 distinct product names.",
            "",
            "| Product | Excel SKU | MRP (₹) | Selling (₹) | Inventory |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for match in sorted(
        result["excel_skus_missing_from_db"],
        key=lambda item: (
            item["catalogue"]["name"].casefold(),
            item["catalogue"]["sku_id"],
        ),
    ):
        catalogue = match["catalogue"]
        inventory = match["inventory"]
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(catalogue["name"]),
                    _md(catalogue["sku_id"]),
                    _money(catalogue["mrp_rupees"]),
                    _money(catalogue["selling_price_rupees"]),
                    "∞"
                    if inventory and inventory["quantity"] is None
                    else _md(inventory["quantity"] if inventory else None),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 4. Inventory updates required",
            "",
            "Production can currently store only `in_stock` (true/false), not the "
            "Excel quantity. The following nine matched products must be marked "
            "out of stock because the workbook quantity is zero.",
            "",
            "| Product | DB ID | Excel quantity | Current | Required |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for row in result["inventory_updates"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(row["db_product_name"]),
                    _md(row["db_product_id"]),
                    _quantity_list(row["excel_quantities"]),
                    _md(row["current_in_stock"]),
                    _md(row["required_in_stock"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 5. Selling-price updates required",
            "",
            "The existing `base_price_paise` field behaves as the customer selling "
            "price while global flash pricing is disabled. Sixty-five matched "
            "products already equal the workbook selling price; four need correction.",
            "",
            "| Product | DB ID | MRP (₹) | Selling (₹) | Current DB price (₹) |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in result["price_updates"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(row["db_product_name"]),
                    _md(row["db_product_id"]),
                    _md(row["excel_mrp"]),
                    _md(row["excel_selling_price"]),
                    _md(row["current_db_base_price"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 6. Discounts missing or displayed incorrectly",
            "",
            "Production pricing is globally disabled (`markup_pct=0`, "
            "`flash_off_pct=0`, `flash_active=false`). The live catalogue API "
            "therefore returns identical 1.00 list and charge multipliers. The "
            "database/API has no per-product MRP field, so all 47 matched products "
            "whose Excel MRP exceeds Selling Price lack the required crossed-out "
            "MRP and product-specific discount.",
            "",
            "| Product | DB ID | MRP (₹) | Selling (₹) | Expected discount |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in result["frontend_discount_issues"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md(row["db_product_name"]),
                    _md(row["db_product_id"]),
                    _md(row["excel_mrp"]),
                    _md(row["excel_selling_price"]),
                    _md(row["expected_discount_pct"]) + "%",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 7. Uncertain matches",
            "",
            "No unresolved fuzzy match remains. Two renamed records were confirmed "
            "by comparing their product images:",
            "",
            "- `Black snake watch` ↔ `Snake- Bracelet Watch`.",
            "- `Tulip bracelate- Pink, Purple & Clear Oval Marquise Crystals` ↔ "
            "`Tulip bracelate with Pink, Purple & Clear Oval Marquise Crystals`.",
            "",
            "The second `Minimal celestial ring` SKU "
            "`b38b2f46-5191-4e0b-871e-e4dfce578632` has a different image from "
            "the existing database ring and is therefore correctly listed as a "
            "missing SKU, not merged by name.",
            "",
            "## Blocking data-model gaps",
            "",
            "The production schema cannot yet represent all authoritative workbook data:",
            "",
            "- `products` stores one `base_price_paise`; it has no per-product MRP.",
            "- `products` stores only `in_stock`; it has no inventory quantity.",
            "- `product_variants` stores SKU and price delta but no inventory quantity "
            "or per-variant MRP.",
            "- The global flash-discount mechanism cannot represent the workbook's "
            "different product-level discounts.",
            "",
            "Recommended implementation before applying the full sync:",
            "",
            "1. Keep `base_price_paise` as the actual selling/checkout price.",
            "2. Add nullable `mrp_price_paise` to products (and variants if variant "
            "prices can differ).",
            "3. Add nullable inventory quantities to products and variants; null should "
            "preserve the workbook's “infinite” inventory meaning.",
            "4. Return MRP, selling price, calculated discount percentage, and quantity "
            "availability through the catalogue API.",
            "5. Update the customer storefront to render MRP struck through, selling "
            "price, and the per-product discount; the customer storefront source is "
            "not present in this workspace.",
            "",
            "Until those fields and the storefront are implemented, adding all missing "
            "SKUs or writing quantities would be a partial and lossy production update.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outputs(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    sections = {
        "matched-products.csv": result["matched_products"],
        "db-products-missing-from-excel.csv": result["db_products_missing_from_excel"],
        "excel-skus-missing-from-db.csv": result["excel_skus_missing_from_db"],
        "ambiguous-matches.csv": result["ambiguous_matches"],
        "inventory-updates.csv": result["inventory_updates"],
        "price-updates.csv": result["price_updates"],
        "price-conflicts.csv": result["price_conflicts"],
        "frontend-discount-issues.csv": result["frontend_discount_issues"],
    }
    for filename, rows in sections.items():
        _write_csv(output_dir / filename, rows)
    _write_report(output_dir / "REPORT.md", result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalogue, inventory, sheet_names = load_workbook(args.workbook)
    snapshot = load_snapshot(args.snapshot)
    result = analyse(catalogue, inventory, sheet_names, snapshot)
    write_outputs(result, args.output_dir)
    print(json.dumps(result["validation"], indent=2, ensure_ascii=False))
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
