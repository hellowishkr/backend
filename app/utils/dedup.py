"""Gift deduplication — check what's already been purchased for an occasion.

Tracks BOTH internal Wishkr orders AND external self-reported claims
so friends don't duplicate gifts regardless of where they were bought.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExternalGiftClaim, Purchase, PurchaseStatus


async def get_purchased_categories(db: AsyncSession, occasion_id: str) -> dict[str, list[str]]:
    """
    Get gift types and brand names already purchased via Wishkr for an occasion.

    Returns: { "GIFT_CARD": ["Starbucks", "Amazon"], "FLOWERS": [] }
    """
    result = await db.execute(
        select(Purchase.gift_type, Purchase.brand_name).where(
            Purchase.occasion_id == occasion_id,
            Purchase.status.in_([PurchaseStatus.VOUCHER_ISSUED, PurchaseStatus.DELIVERED]),
        )
    )
    rows = result.all()

    categories: dict[str, list[str]] = {}
    for gift_type, brand_name in rows:
        key = gift_type.value if hasattr(gift_type, "value") else str(gift_type)
        if key not in categories:
            categories[key] = []
        if brand_name:
            categories[key].append(brand_name)

    return categories


async def get_full_gift_checklist(db: AsyncSession, occasion_id: str) -> dict:
    """Unified view: internal Wishkr purchases + external self-reported claims.

    Returns:
        {
            "internal": [{"type": "GIFT_CARD", "brand": "Amazon", "amount": 1000}],
            "external": [{"description": "Prada bag", "category": "fashion", "brand": "Prada"}],
            "categories_taken": ["GIFT_CARD", "fashion"],
            "brands_taken": ["Amazon", "Prada"],
        }
    """
    # Internal purchases
    internal_rows = await db.execute(
        select(Purchase.gift_type, Purchase.brand_name, Purchase.amount).where(
            Purchase.occasion_id == occasion_id,
            Purchase.status.in_([PurchaseStatus.VOUCHER_ISSUED, PurchaseStatus.DELIVERED]),
        )
    )
    internal = []
    categories_taken: set[str] = set()
    brands_taken: set[str] = set()
    for gtype, brand, amount in internal_rows.all():
        gkey = gtype.value if hasattr(gtype, "value") else str(gtype)
        internal.append({"type": gkey, "brand": brand, "amount": float(amount) if amount else None})
        categories_taken.add(gkey)
        if brand:
            brands_taken.add(brand.lower())

    # External claims
    ext_rows = await db.execute(
        select(ExternalGiftClaim).where(ExternalGiftClaim.occasion_id == occasion_id)
    )
    external = []
    for claim in ext_rows.scalars().all():
        external.append({
            "description": claim.item_description,
            "category": claim.category,
            "brand": claim.brand_name,
        })
        if claim.category:
            categories_taken.add(claim.category.lower())
        if claim.brand_name:
            brands_taken.add(claim.brand_name.lower())

    return {
        "internal": internal,
        "external": external,
        "categories_taken": sorted(categories_taken),
        "brands_taken": sorted(brands_taken),
    }
