"""Configure PyariSakhee with one flat ₹59 shipping charge per order.

The script is intentionally vendor-specific and idempotent:
- requires the known production vendor id and name;
- sets the vendor flat shipping fee to 5,900 paise;
- disables the legacy per-extra-unit vendor fee;
- removes product-level shipping from every product owned by the vendor;
- defaults to a dry run and requires ``--commit`` to persist.

Run only after the migration adding ``vendors.shipping_flat_fee_paise``.
"""

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy import select


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
from app.models.product import Product
from app.models.vendor import Vendor


VENDOR_ID = "ven_1782830020_c1f45d4d"
EXPECTED_VENDOR_NAME = "PyariSakhee"
FLAT_SHIPPING_FEE_PAISE = 5_900


async def configure_shipping(*, commit: bool) -> None:
    async with async_session() as db:
        vendor = await db.get(Vendor, VENDOR_ID)
        if vendor is None:
            raise RuntimeError(f"Required production vendor {VENDOR_ID!r} was not found")
        if vendor.name != EXPECTED_VENDOR_NAME:
            raise RuntimeError(
                f"Vendor {VENDOR_ID!r} is named {vendor.name!r}; "
                f"expected {EXPECTED_VENDOR_NAME!r}"
            )

        products = (
            await db.execute(
                select(Product).where(Product.vendor_id == VENDOR_ID).order_by(Product.id)
            )
        ).scalars().all()
        products_with_shipping = [
            product
            for product in products
            if not product.shipping_included or product.shipping_fee_paise is not None
        ]

        print(f"Vendor: {vendor.id} ({vendor.name})")
        print(f"Vendor products: {len(products)}")
        print(
            "Vendor shipping before: "
            f"flat={vendor.shipping_flat_fee_paise!r}, "
            f"extra_item={vendor.shipping_extra_item_fee_paise} paise"
        )
        print(f"Products with product-level shipping to clear: {len(products_with_shipping)}")
        print(f"Vendor shipping after: flat={FLAT_SHIPPING_FEE_PAISE} paise, extra_item=0 paise")
        print("Order shipping for quantities 1 and 100: 5900 paise")

        vendor.shipping_flat_fee_paise = FLAT_SHIPPING_FEE_PAISE
        vendor.shipping_extra_item_fee_paise = 0
        for product in products:
            product.shipping_included = True
            product.shipping_fee_paise = None

        if commit:
            await db.commit()
            print("COMMITTED")
        else:
            await db.rollback()
            print("DRY RUN ONLY - transaction rolled back; pass --commit to persist")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set PyariSakhee shipping to a flat ₹59 per order."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist changes. Without this flag the transaction is rolled back.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(configure_shipping(commit=args.commit))


if __name__ == "__main__":
    main()
