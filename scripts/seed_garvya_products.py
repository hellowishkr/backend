"""Seed Garvya physical product catalog rows."""

import asyncio
from decimal import Decimal
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def load_local_database_url() -> None:
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

from app.db.session import async_session
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor


GARVYA_VENDOR_ID = "ven_garvya"


# Real data from Garvya's email (verified 2026-06-19):
# Candle ₹599, Room Spray ₹499, customization +₹150.
# Candle scents: Sandalwood, Waterlily, Vanilla, Lavender, Rose, Jasmine.
# Spray scents: Breeze, Rose, Jasmine, Lavender.
# NOTE: prices here are Garvya's base prices. If shipping is folded into the
# listed price per the commission decision, confirm the shipping-inclusive
# price with Garvya before going live and update base_price_paise accordingly.
PRODUCTS = [
    {
        "id": "prd_garvya_candle",
        "name": "Scented Soywax Candle",
        "description": "Hand-poured scented soywax candle for festive and personal gifting.",
        "base_price_paise": 59900,  # ₹599
        "gst_rate": Decimal("12.00"),
        "hsn": "3406",
        "image_url": None,
        "variants": [
            ("var_garvya_candle_sandalwood", "Sandalwood", "GARVYA-CANDLE-SANDALWOOD", 0),
            ("var_garvya_candle_waterlily", "Waterlily", "GARVYA-CANDLE-WATERLILY", 0),
            ("var_garvya_candle_vanilla", "Vanilla", "GARVYA-CANDLE-VANILLA", 0),
            ("var_garvya_candle_lavender", "Lavender", "GARVYA-CANDLE-LAVENDER", 0),
            ("var_garvya_candle_rose", "Rose", "GARVYA-CANDLE-ROSE", 0),
            ("var_garvya_candle_jasmine", "Jasmine", "GARVYA-CANDLE-JASMINE", 0),
        ],
    },
    {
        "id": "prd_garvya_room_spray",
        "name": "Room Spray",
        "description": "Premium room spray for home fragrance and gift hampers.",
        "base_price_paise": 49900,  # ₹499
        "gst_rate": Decimal("18.00"),
        "hsn": "3307",
        "image_url": None,
        "variants": [
            ("var_garvya_spray_breeze", "Breeze", "GARVYA-SPRAY-BREEZE", 0),
            ("var_garvya_spray_rose", "Rose", "GARVYA-SPRAY-ROSE", 0),
            ("var_garvya_spray_jasmine", "Jasmine", "GARVYA-SPRAY-JASMINE", 0),
            ("var_garvya_spray_lavender", "Lavender", "GARVYA-SPRAY-LAVENDER", 0),
        ],
    },
]


# Garvya's customization is a single +₹150 option (label/color/message).
ADDONS = [
    ("add_garvya_custom", "Customization (label / color / message)", 15000),  # ₹150
]

ADDON_PRODUCT_SUFFIXES = {
    "prd_garvya_candle": "candle",
    "prd_garvya_room_spray": "spray",
}


async def upsert_vendor(db: AsyncSession) -> None:
    vendor = await db.get(Vendor, GARVYA_VENDOR_ID)
    if vendor is None:
        vendor = Vendor(
            id=GARVYA_VENDOR_ID,
            name="Garvya",
            category="Home Fragrance",
            contact_info=None,
            razorpay_account_id=None,
            fulfillment_mode="manual",
            active=True,
        )
        db.add(vendor)
    else:
        vendor.name = "Garvya"
        vendor.category = "Home Fragrance"
        vendor.fulfillment_mode = "manual"
        vendor.active = True


async def upsert_product(db: AsyncSession, product_data: dict) -> Product:
    product = await db.get(Product, product_data["id"])
    if product is None:
        product = Product(id=product_data["id"], vendor_id=GARVYA_VENDOR_ID)
        db.add(product)

    product.name = product_data["name"]
    product.description = product_data["description"]
    product.base_price_paise = product_data["base_price_paise"]
    product.gst_rate = product_data["gst_rate"]
    product.hsn = product_data["hsn"]
    product.image_url = product_data["image_url"]
    product.active = True
    return product


async def upsert_variant(
    db: AsyncSession,
    product_id: str,
    variant_id: str,
    name: str,
    sku: str,
    price_delta_paise: int,
    sort_order: int,
) -> None:
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        variant = ProductVariant(id=variant_id, product_id=product_id)
        db.add(variant)

    variant.name = name
    variant.sku = sku
    variant.price_delta_paise = price_delta_paise
    variant.sort_order = sort_order
    variant.active = True


async def upsert_addon(
    db: AsyncSession,
    product_id: str,
    addon_id: str,
    name: str,
    price_paise: int,
    sort_order: int,
) -> None:
    addon = await db.get(ProductAddon, addon_id)
    if addon is None:
        addon = ProductAddon(id=addon_id, product_id=product_id)
        db.add(addon)

    addon.name = name
    addon.price_paise = price_paise
    addon.sort_order = sort_order
    addon.active = True


async def seed() -> None:
    async with async_session() as db:
        await upsert_vendor(db)

        for product_data in PRODUCTS:
            product = await upsert_product(db, product_data)
            for sort_order, variant in enumerate(product_data["variants"], start=1):
                await upsert_variant(db, product.id, *variant, sort_order=sort_order)
            for sort_order, (addon_id, name, price_paise) in enumerate(ADDONS, start=1):
                suffix = ADDON_PRODUCT_SUFFIXES[product.id]
                await upsert_addon(
                    db,
                    product.id,
                    f"{addon_id}_{suffix}",
                    name,
                    price_paise,
                    sort_order,
                )

        await db.commit()

        products = (
            await db.execute(select(Product).where(Product.vendor_id == GARVYA_VENDOR_ID))
        ).scalars().all()
        print(f"Seeded Garvya catalog: {len(products)} products")


if __name__ == "__main__":
    asyncio.run(seed())
