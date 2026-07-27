from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.catalog import serialize_product
from app.core.config import settings
from app.models.product import Product
from app.models.vendor import Vendor


async def list_active_catalog_products_for_bot(db: AsyncSession) -> list[dict]:
    """Future WhatsApp catalog source. Deliberately gated off for Epic 6."""
    if not settings.bot_product_catalog_enabled:
        return []

    result = await db.execute(
        select(Product)
        .join(Vendor)
        .where(Product.active.is_(True), Vendor.active.is_(True))
        .options(
            selectinload(Product.vendor),
            selectinload(Product.variants),
            selectinload(Product.addons),
        )
        .order_by(Product.name)
    )
    return [serialize_product(product) for product in result.scalars().unique().all()]
