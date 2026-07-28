"""Canonical URLs for products shown outside the Wishkr storefront."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

# These are the curated category facets in wishkr-web's SHOP_FACETS. Categories
# without a curated landing page intentionally use the canonical flat route.
_CATEGORY_FACET_SLUGS = {
    "Cakes": "cakes",
    "Candles": "candles",
    "Chocolates": "chocolates",
    "Flowers": "flowers",
    "Hampers": "hampers",
    "Jewellery": "jewellery",
    "Personalized": "personalized",
}


def _canonical_storefront_base(base_url: str) -> str:
    """Return the configured origin, avoiding Wishkr's bare-host redirect."""
    candidate = (base_url or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.hostname and parsed.hostname.lower() == "wishkr.com":
        port = f":{parsed.port}" if parsed.port else ""
        parsed = parsed._replace(
            scheme="https",
            netloc=f"www.wishkr.com{port}",
        )
        candidate = urlunsplit(parsed)
    return candidate


def _product_name_slug(name: str) -> str:
    """Match wishkr-web's slugifyName contract for canonical product routes."""
    value = unicodedata.normalize("NFKD", (name or "").lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:60]


def wishkr_product_url(
    *,
    base_url: str,
    product_id: str,
    product_name: str,
    category: str | None,
) -> str:
    """Build the exact canonical storefront URL used by wishkr-web."""
    base = _canonical_storefront_base(base_url)
    name_slug = _product_name_slug(product_name)
    product_segment = f"{name_slug}-{product_id}" if name_slug else product_id
    category_slug = _CATEGORY_FACET_SLUGS.get(category or "")
    if category_slug:
        return f"{base}/shop/{category_slug}/{product_segment}"
    return f"{base}/shop/{product_segment}"
