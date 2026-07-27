"""Export a read-only production snapshot for the PyaariSakhee catalogue audit.

The script prints JSON between stable markers so operational Docker output can
be separated from the snapshot. It never commits or mutates database state.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import selectinload

from app.db.session import async_session, engine
from app.models.admin_user import PricingSetting
from app.models.product import Product
from app.models.vendor import Vendor


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _vendor_name_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.casefold())


def _is_pyaarisakhee(name: str) -> bool:
    key = _vendor_name_key(name)
    return "sakhee" in key and ("pyari" in key or "pyaari" in key)


async def export_snapshot() -> dict[str, Any]:
    async with async_session() as db:
        vendors = (
            (await db.execute(select(Vendor).order_by(Vendor.name, Vendor.id)))
            .scalars()
            .all()
        )
        matching_vendors = [vendor for vendor in vendors if _is_pyaarisakhee(vendor.name)]
        if not matching_vendors:
            available = [
                {"id": vendor.id, "name": vendor.name}
                for vendor in vendors
                if "sak" in _vendor_name_key(vendor.name)
            ]
            raise RuntimeError(
                "PyaariSakhee vendor not found; possible vendor matches: "
                + json.dumps(available, ensure_ascii=False)
            )

        vendor_ids = [vendor.id for vendor in matching_vendors]
        products = (
            (
                await db.execute(
                    select(Product)
                    .where(Product.vendor_id.in_(vendor_ids))
                    .options(
                        selectinload(Product.variants),
                        selectinload(Product.addons),
                    )
                    .order_by(Product.name, Product.id)
                )
            )
            .scalars()
            .unique()
            .all()
        )
        pricing = (await db.execute(select(PricingSetting).limit(1))).scalar_one_or_none()

        def product_payload(product: Product) -> dict[str, Any]:
            return {
                "id": product.id,
                "vendor_id": product.vendor_id,
                "name": product.name,
                "description": product.description,
                "category": product.category,
                "occasions": product.occasions or [],
                "base_price_paise": product.base_price_paise,
                "active": product.active,
                "in_stock": product.in_stock,
                "lifecycle_status": product.lifecycle_status,
                "is_bestseller": product.is_bestseller,
                "hsn": product.hsn,
                "image_url": product.image_url,
                "product_images": product.product_images or [],
                "created_at": product.created_at.isoformat() if product.created_at else None,
                "updated_at": product.updated_at.isoformat() if product.updated_at else None,
                "variants": [
                    {
                        "id": variant.id,
                        "name": variant.name,
                        "sku": variant.sku,
                        "price_delta_paise": variant.price_delta_paise,
                        "active": variant.active,
                        "sort_order": variant.sort_order,
                    }
                    for variant in sorted(
                        product.variants, key=lambda item: (item.sort_order, item.id)
                    )
                ],
            }

        snapshot = {
            "vendors": [
                {
                    "id": vendor.id,
                    "name": vendor.name,
                    "active": vendor.active,
                    "onboarding_status": vendor.onboarding_status,
                }
                for vendor in matching_vendors
            ],
            "products": [product_payload(product) for product in products],
            "pricing": (
                {
                    "id": pricing.id,
                    "markup_pct": pricing.markup_pct,
                    "flash_off_pct": pricing.flash_off_pct,
                    "flash_active": pricing.flash_active,
                    "updated_at": (
                        pricing.updated_at.isoformat() if pricing.updated_at else None
                    ),
                }
                if pricing
                else None
            ),
            "database_columns": await db.run_sync(
                lambda sync_session: {
                    table: [
                        {
                            "name": column["name"],
                            "type": str(column["type"]),
                            "nullable": column["nullable"],
                        }
                        for column in inspect(sync_session.connection()).get_columns(table)
                    ]
                    for table in ("products", "product_variants", "pricing_settings")
                }
            ),
        }
        await db.rollback()
        return snapshot


async def _main() -> None:
    try:
        snapshot = await export_snapshot()
        print("SNAPSHOT_JSON_BEGIN")
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=_json_default))
        print("SNAPSHOT_JSON_END")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
