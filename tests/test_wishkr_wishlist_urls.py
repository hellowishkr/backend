from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Product, Vendor, WishlistItem
from app.utils.link_utils import (
    extract_product_id,
    extract_urls,
    is_product_link,
    normalize_url,
)


@pytest.mark.parametrize(
    ("url", "product_id"),
    [
        (
            "https://wishkr.com/shop/prd_1782475741_94ae03da",
            "wishkr:prd_1782475741_94ae03da",
        ),
        (
            "https://www.wishkr.com/shop/customized-candle-gift-hampers-prd_1782475741_94ae03da",
            "wishkr:prd_1782475741_94ae03da",
        ),
        (
            "https://wishkr.com/shop/wedding/customized-candle-prd_1782475741_94ae03da",
            "wishkr:prd_1782475741_94ae03da",
        ),
    ],
)
def test_wishkr_product_urls_are_recognized(url: str, product_id: str) -> None:
    assert is_product_link(url) is True
    assert extract_product_id(url) == product_id


def test_scheme_less_wishkr_product_url_is_extracted() -> None:
    assert extract_urls(
        "save wishkr.com/shop/customized-candle-prd_1782475741_94ae03da"
    ) == [
        "https://wishkr.com/shop/customized-candle-prd_1782475741_94ae03da"
    ]


def test_wishkr_url_normalization_lowercases_the_product_path() -> None:
    mixed_case = (
        "https://WISHKR.com/Shop/Customized-Candle-"
        "PRD_1782475741_94AE03DA?utm_source=whatsapp"
    )
    canonical = (
        "https://wishkr.com/shop/customized-candle-"
        "prd_1782475741_94ae03da"
    )

    assert normalize_url(mixed_case) == canonical
    assert extract_product_id(mixed_case) == (
        "wishkr:prd_1782475741_94ae03da"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://wishkr.com/",
        "https://wishkr.com/shop",
        "https://wishkr.com/about",
        "https://notwishkr.com/shop/prd_1782475741_94ae03da",
    ],
)
def test_non_product_wishkr_urls_are_not_classified_as_products(url: str) -> None:
    assert is_product_link(url) is False




@pytest.mark.asyncio
async def test_wishkr_url_tool_resolves_title_and_deduplicates(db):
    from app.bot.tools.tool_impl.wishlist import add_to_wishlist
    product_id = "prd_1782475741_94ae03da"
    db.add(Vendor(id="vnd_test", name="Seller", category="gifts", active=True))
    db.add(Product(id=product_id, vendor_id="vnd_test", name="Candle Hampers", base_price_paise=159900, active=True))
    await db.flush()
    first = await add_to_wishlist(db, "919000000777", {"url": f"https://wishkr.com/shop/candle-{product_id}"})
    second = await add_to_wishlist(db, "919000000777", {"url": f"https://www.wishkr.com/shop/{product_id}"})
    item = (await db.execute(select(WishlistItem))).scalar_one()
    assert first["success"] and second["duplicate"]
    assert item.product_id == f"wishkr:{product_id}" and item.title == "Candle Hampers"
