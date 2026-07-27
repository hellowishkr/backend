"""Idempotence and safety tests for the authoritative PyariSakhee sync."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.product import Product, ProductAddon, ProductVariant
from app.models.vendor import Vendor
from scripts.sync_pyaarisakhee_catalog_20260725 import (
    CatalogueRow,
    InventoryRow,
    SourceGroup,
    SourceItem,
    product_id_for_sku,
    sync_catalogue,
    variant_id_for_sku,
    variant_sku,
)


def _item(
    sku: str,
    name: str,
    *,
    selling: int,
    mrp: int,
    quantity: int | None,
    relationship: str | None = None,
    colour: str | None = None,
    size: str | None = None,
    description: str | None = None,
    images: tuple[str, ...] | None = None,
) -> SourceItem:
    return SourceItem(
        catalogue=CatalogueRow(
            row=2,
            sku_id=sku,
            custom_sku=None,
            name=name,
            mrp_paise=mrp,
            selling_paise=selling,
            product_category="Traditional earrings",
            description=(
                description
                if description is not None
                else (
                    f"{name} is a carefully finished jewellery piece designed "
                    "for everyday and occasion styling. It makes a thoughtful "
                    "gift, with exact colour, finish, scale and proportions "
                    "shown in the listing images."
                )
            ),
            variant_relationship=relationship,
            size=size,
            colour_code=None,
            colour_name=colour,
            is_bestseller=False,
            hsn=None,
            images=(
                images
                if images is not None
                else (f"https://example.com/{sku}.jpg",)
            ),
        ),
        inventory=InventoryRow(sku_id=sku, quantity=quantity),
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Vendor.__table__,
                Product.__table__,
                ProductVariant.__table__,
                ProductAddon.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sync_updates_creates_preserves_review_candidate_and_is_idempotent(
    session_factory,
):
    existing_sku = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    new_sku = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    parent_sku = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    child_sku = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    existing_id = product_id_for_sku(existing_sku)
    variant_product_id = product_id_for_sku(parent_sku)

    groups = [
        SourceGroup(
            (
                _item(
                    existing_sku,
                    "Updated Earrings",
                    selling=9_900,
                    mrp=14_900,
                    quantity=0,
                ),
            )
        ),
        SourceGroup(
            (
                _item(
                    new_sku,
                    "New Earrings",
                    selling=34_900,
                    mrp=34_900,
                    quantity=1,
                ),
            )
        ),
        SourceGroup(
            (
                _item(
                    parent_sku,
                    "Colour Bracelet",
                    selling=6_200,
                    mrp=6_200,
                    quantity=1,
                    relationship="Parent",
                    colour="Beige",
                ),
                _item(
                    child_sku,
                    "Colour Bracelet",
                    selling=5_900,
                    mrp=6_500,
                    quantity=1,
                    relationship="Child",
                    colour="Black",
                ),
            )
        ),
    ]

    async with session_factory() as db:
        db.add(Vendor(id="ven_ps", name="PyariSakhee", category="Jewellery", active=True))
        db.add(
            Product(
                id=existing_id,
                vendor_id="ven_ps",
                name="Old Earrings",
                description="Old",
                category="Jewellery",
                occasions=[],
                base_price_paise=10_000,
                active=True,
                in_stock=True,
                variants=[],
                addons=[],
            )
        )
        db.add(
            Product(
                id=variant_product_id,
                vendor_id="ven_ps",
                name="Colour Bracelet",
                description="Old",
                category="Jewellery",
                occasions=[],
                base_price_paise=6_200,
                active=True,
                in_stock=True,
                variants=[],
                addons=[],
            )
        )
        db.add(
            Product(
                id="prd_review_only",
                vendor_id="ven_ps",
                name="Not In Workbook",
                base_price_paise=1_000,
                active=True,
                in_stock=True,
                variants=[],
                addons=[],
            )
        )
        await db.commit()

    async with session_factory() as db:
        summary = await sync_catalogue(db, groups)
        await db.commit()

    assert summary.products_created == 1
    assert summary.products_updated == 2
    assert summary.variants_created == 2
    assert summary.missing_from_workbook == [("prd_review_only", "Not In Workbook")]

    async with session_factory() as db:
        updated = await db.get(Product, existing_id)
        created = await db.get(Product, product_id_for_sku(new_sku))
        variant_product = await db.get(Product, variant_product_id)
        variants = (
            await db.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == variant_product_id
                )
            )
        ).scalars().all()

    assert updated is not None
    assert updated.name == "Updated Earrings"
    assert updated.base_price_paise == 14_900
    assert updated.discounted_price_paise == 9_900
    assert updated.inventory_quantity == 0
    assert updated.in_stock is False
    assert created is not None
    assert created.inventory_quantity == 1
    assert variant_product is not None
    assert variant_product.inventory_quantity == 2
    child_variant = next(
        variant for variant in variants if variant.sku == variant_sku(child_sku)
    )
    assert child_variant.price_delta_paise == 6_500
    assert child_variant.discounted_price_paise == 5_900

    async with session_factory() as db:
        rerun = await sync_catalogue(db, groups)
        await db.rollback()

    assert rerun.total_changes == 0
    assert rerun.products_created == 0
    assert rerun.products_updated == 0
    assert rerun.missing_from_workbook == [("prd_review_only", "Not In Workbook")]


@pytest.mark.asyncio
async def test_sync_preserves_rich_description_and_wishkr_images(
    session_factory,
):
    existing_sku = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    new_sku = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    existing_id = product_id_for_sku(existing_sku)
    rich_description = (
        "<p>A detailed floral bracelet with polished crystal accents for "
        "occasion styling. It is a thoughtful gift for birthdays, "
        "anniversaries and romantic celebrations, with the exact finish and "
        "proportions shown in the listing images.</p>"
    )
    wishkr_image = "https://product-catalog.wishkr.com/vendor-uploads/ven_ps/tulip.webp"
    groups = [
        SourceGroup(
            (
                _item(
                    existing_sku,
                    "Tulip Crystal Bracelet",
                    selling=34_900,
                    mrp=39_900,
                    quantity=2,
                    description="Tulip Crystal Bracelet",
                    images=("https://m.media-amazon.com/tulip.jpg",),
                ),
            )
        ),
        SourceGroup(
            (
                _item(
                    new_sku,
                    "Ikshita Gold Jhumki Earrings",
                    selling=19_900,
                    mrp=29_900,
                    quantity=1,
                    description="Ikshita Gold Jhumki Earrings",
                ),
            )
        ),
    ]

    async with session_factory() as db:
        db.add(
            Vendor(
                id="ven_ps",
                name="PyariSakhee",
                category="Jewellery",
                active=True,
            )
        )
        db.add(
            Product(
                id=existing_id,
                vendor_id="ven_ps",
                name="Tulip Crystal Bracelet",
                description=rich_description,
                category="Jewellery",
                occasions=[],
                product_images=[wishkr_image],
                image_url=wishkr_image,
                base_price_paise=42_900,
                active=True,
                in_stock=True,
                variants=[],
                addons=[],
            )
        )
        await db.commit()

    async with session_factory() as db:
        await sync_catalogue(db, groups)
        await db.commit()

    async with session_factory() as db:
        existing = await db.get(Product, existing_id)
        created = await db.get(Product, product_id_for_sku(new_sku))

    assert existing is not None
    assert existing.description == rich_description
    assert existing.product_images == [wishkr_image]
    assert existing.image_url == wishkr_image
    assert existing.base_price_paise == 39_900
    assert existing.discounted_price_paise == 34_900
    assert created is not None
    assert created.description != created.name
    assert created.description is not None
    assert len(created.description) >= 80


@pytest.mark.asyncio
async def test_sync_omits_free_size_and_preserves_case_equivalent_variant_sku(
    session_factory,
):
    sku = "117efa8e-2995-4f36-88ac-000000000000"
    product_id = product_id_for_sku(sku)
    generated_sku = variant_sku(sku)
    uppercase_sku = generated_sku.upper()
    description = (
        "A stretch bracelet set with crystal rhinestones and offered in a "
        "neutral beige colour. It is designed for everyday and occasion "
        "styling, with the exact finish shown in the listing images."
    )
    item = _item(
        sku,
        "Stretch Crystal Rhinestone Bracelet",
        selling=6_200,
        mrp=6_200,
        quantity=1,
        relationship="Parent",
        colour="Beige",
        size="free",
        description=description,
    )
    groups = [SourceGroup((item,))]

    async with session_factory() as db:
        db.add(
            Vendor(
                id="ven_ps",
                name="PyariSakhee",
                category="Jewellery",
                active=True,
            )
        )
        db.add(
            Product(
                id=product_id,
                vendor_id="ven_ps",
                name=item.catalogue.name,
                description=description,
                category="Jewellery",
                occasions=[
                    "birthday",
                    "anniversary",
                    "rakhi",
                    "just-because",
                ],
                product_images=list(item.catalogue.images),
                image_url=item.catalogue.images[0],
                base_price_paise=6_200,
                discounted_price_paise=6_200,
                inventory_quantity=1,
                active=True,
                in_stock=True,
                variants=[
                    ProductVariant(
                        id=variant_id_for_sku(sku),
                        name="Beige",
                        sku=uppercase_sku,
                        price_delta_paise=6_200,
                        discounted_price_paise=6_200,
                        inventory_quantity=1,
                        active=True,
                        sort_order=1,
                    )
                ],
                addons=[],
            )
        )
        await db.commit()

    async with session_factory() as db:
        summary = await sync_catalogue(db, groups)
        await db.commit()

    async with session_factory() as db:
        variant = await db.get(ProductVariant, variant_id_for_sku(sku))

    assert variant is not None
    assert variant.name == "Beige"
    assert variant.sku == uppercase_sku
    assert summary.variants_updated == 0
    assert summary.field_changes.get("sku", 0) == 0
