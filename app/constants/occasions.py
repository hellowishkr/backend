"""Canonical gifting occasions for the storefront.

Drives the Shop-by-Occasion tiles, listing filter, and per-product tagging
(see Product.occasions). Keep in sync with the storefront occasion list and
the mockup (docs/build/wishkr-gifting-mockup.html).
"""

# Display label -> stable slug. Slugs are stored in Product.occasions.
GIFTING_OCCASIONS: dict[str, str] = {
    "Birthday": "birthday",
    "Anniversary": "anniversary",
    "Diwali": "diwali",
    "Rakhi": "rakhi",
    "Just Because": "just-because",
    "Corporate": "corporate",
}

OCCASION_SLUGS: frozenset[str] = frozenset(GIFTING_OCCASIONS.values())
