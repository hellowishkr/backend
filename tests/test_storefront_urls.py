"""Tests for canonical Wishkr storefront URLs shared outside the web app."""

from app.utils.storefront_urls import wishkr_product_url


def test_product_url_matches_the_web_canonical_route() -> None:
    assert wishkr_product_url(
        base_url="https://wishkr.com",
        product_id="prd_ps_29069dde67ff4c0aa673",
        product_name="Aboli Jhumki Earrings",
        category="Jewellery",
    ) == (
        "https://www.wishkr.com/shop/jewellery/"
        "aboli-jhumki-earrings-prd_ps_29069dde67ff4c0aa673"
    )


def test_product_url_uses_flat_canonical_route_for_uncurated_category() -> None:
    assert wishkr_product_url(
        base_url="https://staging.wishkr.com",
        product_id="prd_gaming_lamp",
        product_name="Gaming Desk Lamp",
        category="Gaming",
    ) == (
        "https://staging.wishkr.com/shop/"
        "gaming-desk-lamp-prd_gaming_lamp"
    )


def test_product_url_matches_web_slug_normalisation() -> None:
    assert wishkr_product_url(
        base_url="https://www.wishkr.com/",
        product_id="prd_cafe",
        product_name="Café & Candle Gift — Deluxe",
        category="Candles",
    ) == (
        "https://www.wishkr.com/shop/candles/"
        "cafe-and-candle-gift-deluxe-prd_cafe"
    )
