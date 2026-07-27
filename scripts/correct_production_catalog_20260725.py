"""Correct the production product catalogue audited on 2026-07-25.

The correction is intentionally explicit and conservative:

- adds the missing Home Fragrance and Home Décor & Keepsakes categories;
- corrects 12 product category assignments;
- sets reviewed occasion mappings for 75 products;
- replaces 59 inadequate product descriptions;
- validates every target product name and every expected old value;
- defaults to a dry run and requires ``--commit`` to persist;
- invalidates the storefront product and taxonomy caches after a commit.

The expected-value guards make the script safe to rerun and prevent it from
overwriting edits made after the audit. A field may be in either its audited
state or its final state; any third value aborts the entire transaction.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def load_local_database_url() -> None:
    """Use .env.dev for local dry runs while respecting injected production env."""
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

from app.api.catalog import PRODUCTS_CACHE_KEY, TAXONOMY_CACHE_KEY
from app.db.session import async_session
from app.models.catalog_taxonomy import GiftingOccasion, ProductCategory
from app.models.product import Product
from app.utils.redis_client import get_redis


@dataclass(frozen=True)
class FieldUpdate:
    expected: Any
    desired: Any


@dataclass(frozen=True)
class CategorySpec:
    id: str
    name: str
    sort_order: int


@dataclass(frozen=True)
class CorrectionSummary:
    categories_created: int
    product_categories_changed: int
    occasions_changed: int
    descriptions_changed: int

    @property
    def total_changes(self) -> int:
        return (
            self.categories_created
            + self.product_categories_changed
            + self.occasions_changed
            + self.descriptions_changed
        )


NEW_CATEGORIES = (
    CategorySpec("home-fragrance", "Home Fragrance", 8),
    CategorySpec("home-decor-keepsakes", "Home Décor & Keepsakes", 9),
)

CANONICAL_OCCASION_IDS = {
    "birthday",
    "anniversary",
    "wedding",
    "engagement",
    "proposal",
    "romantic-surprise",
    "valentines-day",
    "mothers-day",
    "fathers-day",
    "diwali",
    "rakhi",
    "bhai-dooj",
    "karwa-chauth",
    "holi",
    "eid",
    "christmas",
    "new-year",
    "housewarming",
    "baby-shower",
    "congratulations",
    "thank-you",
    "get-well-soon",
    "farewell",
    "corporate",
    "premium-gifting",
    "just-because",
}


PRODUCT_NAMES = {
    # Garvya
    "prd_gv_s_breeze": "Coastal Breeze Room Spray",
    "prd_gv_s_laven": "Lavender Room Spray",
    "prd_gv_s_njasmine": "Night Jasmine Room Spray",
    "prd_gv_s_rose": "Rose Room Spray",
    # Kala Avishkar
    "prd_1783338163_0cc56810": "Custom festive hamper",
    "prd_1784268711_86d686e6": "Elegant handcrafted floral hamper",
    "prd_1783338172_1696145c": "Luxurious floral handmade Toran",
    "prd_1784268770_7332d961": "Raksha Bandhan Primium Hamper",
    # Kats Festive Candles
    "prd_1783521966_14ee036d": "Peony candle",
    # Little. Whittle
    "prd_1783265395_24c483f5": "Jewellery & Keepsake Box – Temple Collection",
    "prd_1783268600_6948e770": "Laxmi abundance box",
    "prd_1783253331_b21ba711": "Royal MDF Jewellery & Keepsake Box",
    # Pratiksha Decore
    "prd_1782475741_891f9818": "Candle Bouquet",
    "prd_1782475742_b52cb41d": "Custom Tin Candles",
    "prd_1782475743_c0ba4893": "Custom Tin Candles",
    "prd_1782475742_d589d0b4": "Peony Flower Candle with Ceramic Bubble Tray",
    "prd_1782475741_4b0bf0f0": "Premium Fragrance Jar Candle",
    "prd_1782475742_262c0e73": "Premium Luxury Bouquet Candle",
    "prd_1782475743_52b74e40": "Rose Heart Teddy Candle",
    # house.ofzahraa
    "prd_1783314686_c10a9ad7": "25K Note Cake Hamper",
    "prd_1783313230_b36abebc": "Birthday Combo",
    # PyariSakhee
    "prd_ps_b99fe3720d684d62a4c6": (
        "Antique Silver Tribal Dangle Earrings with Pink Enamel/Stone Accents"
    ),
    "prd_ps_a4eb10f333e34ace93c1": "Beach celestial ring",
    "prd_ps_1e2f4d9c3ad443b68f58": "Curved leaves celestial ring",
    "prd_ps_ecc5a3c9a3584f68a10b": "Dainty Hollow Heart Pendant Necklace",
    "prd_ps_2a448fe8d5d64282847c": "Delicate Enamel Rose Necklace",
    "prd_ps_eda4ed236f404c45b1b1": "Edge ring",
    "prd_ps_efa0f504313b43b8885d": "Every day ring",
    "prd_ps_1689746a749441fe9a0f": "Everyday celestial ring",
    "prd_ps_cfa0b882a30548439388": "Eye ring",
    "prd_ps_72dc4efe2ae344caadfc": "Fashion ring",
    "prd_ps_3bd2f098d60e432e81e8": "Flower celestial ring",
    "prd_ps_d5c60badede1454aa7e2": "Flower celestial ring",
    "prd_ps_5f48717cb00745b8bdf1": "Galaxy celestial ring",
    "prd_ps_6f5eaca0f520478e8016": "Geometric Fashion Bracelet",
    "prd_ps_68e3bcb5968a423d9e67": (
        "German Silver Chandbali Style Earrings with Sky Blue Stones & Ghungroo Hangings"
    ),
    "prd_ps_654e0414c65e41e387d7": ("German Silver Jhumka Earrings with Black Monalisa Stone"),
    "prd_ps_16cfd7df882244bbb8b3": "Gold plated stainless steel stud earrings",
    "prd_ps_fe0f74a38b7b401aa5b7": ("Gold plated stainless steel sun shaped earrings"),
    "prd_ps_4a8a957c987b4220b879": "Gold teardrop pearl stud earrings",
    "prd_ps_3464fd9bcafc46539f98": ("Gold-plated Kashmiri Jhumka earrings with pearl and stone"),
    "prd_ps_3796c422b60345568841": "Heart sequence ring",
    "prd_ps_2328567897e64e8e86ca": "Heart shaped stud earrings",
    "prd_ps_254da2fd18e346a6a2cc": "Hoops earrings",
    "prd_ps_3aaacef09ac34934a4e2": "Hoops earrings",
    "prd_ps_6907b22128f0415c8827": "Hoops earrings",
    "prd_ps_7c141a4ced2840bb9b7a": "Hoops earrings",
    "prd_ps_87ed07994335442c9304": "Hoops earrings",
    "prd_ps_8c8df5d7540c415dab55": "Hoops earrings",
    "prd_ps_e0494d200bfc486aac0f": "Hoops earrings",
    "prd_ps_f0df411dd6654ab3b2b2": "Hoops earrings",
    "prd_ps_f6c381768c914515ac6b": "Hoops earrings",
    "prd_ps_fadc7308302644fab56f": "Hoops earrings",
    "prd_ps_97fcc7d14f454904bf85": "Korean Earrings",
    "prd_ps_9687b253f08a4cb68efa": "Leaf cascade ring",
    "prd_ps_4a3ba27f35f647e4a073": "Leaf celestial ring",
    "prd_ps_3eae7f7cb88548caa92a": "Lucky Charm Horseshoe Necklace",
    "prd_ps_3e18726fce7a4377b089": ("Mairah Ocean Inspired Starfish Pendant Chain"),
    "prd_ps_dca0581055d44d2e85ca": "Minimal celestial ring",
    "prd_ps_7f035fd8f006476fb026": "Minimal leaf ring",
    "prd_ps_e4d5bf2c12674533b0dc": "Moon star celestial ring",
    "prd_ps_e228270473ce4c3a9005": "Multilayer ring",
    "prd_ps_eb4ffb2042964d8fa78d": ("Mustard Yellow Stone Oxidized Silver Earrings"),
    "prd_ps_d96dce8a8bd047afb6e5": (
        "Oxidized Silver Filigree Earrings with Yellow Stone & Ghungroo Dangles"
    ),
    "prd_ps_80327c9f819a4897ae3f": "Oxidized Silver Teal Stone Earrings",
    "prd_ps_28f639768fbf46e3a7f1": ("Oxidized Silver-Plated Magenta Pink Pear Stone Earrings"),
    "prd_ps_df2100b394304d669d46": "Paper Clip Necklace",
    "prd_ps_64ca9e7453d449c38d62": "Paperclip Chain Heart Charm Necklace",
    "prd_ps_2e246edc9aff48aa964c": "Pearl fashion bracelet",
    "prd_ps_8a846d1e0ff84e5593f3": "Pretty heart necklace",
    "prd_ps_52e6263cbd594e6d904e": ("Retro Long Minimalist Pendant Chain Necklace"),
    "prd_ps_88e7599b869c445d8c6d": "Rhinestone Bangle kada",
    "prd_ps_285bb4c80e504e7a9344": (
        "Royal Doli Motif Gold-Tone Temple Earrings with Pearl Danglers"
    ),
    "prd_ps_3a3b68dfe7a94dac9c35": "Shiny triple hoop earrings",
    "prd_ps_06325dd434d04bc98352": ("Side view of oxidized silver pink stone pearl drop earrings"),
    "prd_ps_ecfa4a6f62f64c849e5d": "Solyn Curved Arc Necklace",
    "prd_ps_e634d912a93a4528b3b5": "Soulmate Heart Necklace",
    "prd_ps_a243069a547845fdbb05": "Sparkling Square Link Bracelate",
    "prd_ps_cc26d558044546e096bc": ("Sparqle Delicate Gold Heart Pendant Necklace"),
    "prd_ps_117efa8e29954f3688ac": (
        "Stretch Crystal Rhinestone Bracelet (color options available)"
    ),
    "prd_ps_6e2d30c0d62643d98b9c": "Sunflower ring",
    "prd_ps_758186c118154b90abbe": (
        "Traditional Indian oxidized silver plated dangle earrings with lavender square"
    ),
    "prd_ps_f7a38b66fde7456bb7f0": ("Vintage Charm Oxidized Silver Black Stone Earrings"),
    "prd_ps_2e06df229a5f49eeb67d": "korean earrings",
}


CATEGORY_UPDATES = {
    "prd_gv_s_breeze": FieldUpdate("Candles", "Home Fragrance"),
    "prd_gv_s_laven": FieldUpdate("Candles", "Home Fragrance"),
    "prd_gv_s_njasmine": FieldUpdate("Candles", "Home Fragrance"),
    "prd_gv_s_rose": FieldUpdate("Candles", "Home Fragrance"),
    "prd_1784268711_86d686e6": FieldUpdate("Chocolates", "Hampers"),
    "prd_1784268770_7332d961": FieldUpdate("Chocolates", "Hampers"),
    "prd_1783338172_1696145c": FieldUpdate("Personalized", "Home Décor & Keepsakes"),
    "prd_1783265395_24c483f5": FieldUpdate("Personalized", "Home Décor & Keepsakes"),
    "prd_1783268600_6948e770": FieldUpdate("Personalized", "Home Décor & Keepsakes"),
    "prd_1783253331_b21ba711": FieldUpdate("Personalized", "Home Décor & Keepsakes"),
    "prd_1783314686_c10a9ad7": FieldUpdate("Cakes", "Hampers"),
    "prd_1783313230_b36abebc": FieldUpdate("Flowers", "Hampers"),
}


TRADITIONAL_EARRINGS = (
    "prd_ps_b99fe3720d684d62a4c6",
    "prd_ps_68e3bcb5968a423d9e67",
    "prd_ps_654e0414c65e41e387d7",
    "prd_ps_3464fd9bcafc46539f98",
    "prd_ps_eb4ffb2042964d8fa78d",
    "prd_ps_d96dce8a8bd047afb6e5",
    "prd_ps_80327c9f819a4897ae3f",
    "prd_ps_28f639768fbf46e3a7f1",
    "prd_ps_285bb4c80e504e7a9344",
    "prd_ps_06325dd434d04bc98352",
    "prd_ps_758186c118154b90abbe",
    "prd_ps_f7a38b66fde7456bb7f0",
)

MODERN_EARRINGS = (
    "prd_ps_16cfd7df882244bbb8b3",
    "prd_ps_fe0f74a38b7b401aa5b7",
    "prd_ps_4a8a957c987b4220b879",
    "prd_ps_254da2fd18e346a6a2cc",
    "prd_ps_3aaacef09ac34934a4e2",
    "prd_ps_6907b22128f0415c8827",
    "prd_ps_7c141a4ced2840bb9b7a",
    "prd_ps_87ed07994335442c9304",
    "prd_ps_8c8df5d7540c415dab55",
    "prd_ps_e0494d200bfc486aac0f",
    "prd_ps_f0df411dd6654ab3b2b2",
    "prd_ps_f6c381768c914515ac6b",
    "prd_ps_fadc7308302644fab56f",
    "prd_ps_97fcc7d14f454904bf85",
    "prd_ps_3a3b68dfe7a94dac9c35",
    "prd_ps_2e06df229a5f49eeb67d",
)

HEART_JEWELLERY = (
    "prd_ps_ecc5a3c9a3584f68a10b",
    "prd_ps_3796c422b60345568841",
    "prd_ps_2328567897e64e8e86ca",
    "prd_ps_64ca9e7453d449c38d62",
    "prd_ps_8a846d1e0ff84e5593f3",
    "prd_ps_e634d912a93a4528b3b5",
    "prd_ps_cc26d558044546e096bc",
)

FLORAL_JEWELLERY = (
    "prd_ps_2a448fe8d5d64282847c",
    "prd_ps_3bd2f098d60e432e81e8",
    "prd_ps_d5c60badede1454aa7e2",
    "prd_ps_6e2d30c0d62643d98b9c",
)

EVERYDAY_RINGS = (
    "prd_ps_a4eb10f333e34ace93c1",
    "prd_ps_1e2f4d9c3ad443b68f58",
    "prd_ps_eda4ed236f404c45b1b1",
    "prd_ps_efa0f504313b43b8885d",
    "prd_ps_1689746a749441fe9a0f",
    "prd_ps_cfa0b882a30548439388",
    "prd_ps_72dc4efe2ae344caadfc",
    "prd_ps_5f48717cb00745b8bdf1",
    "prd_ps_9687b253f08a4cb68efa",
    "prd_ps_4a3ba27f35f647e4a073",
    "prd_ps_dca0581055d44d2e85ca",
    "prd_ps_7f035fd8f006476fb026",
    "prd_ps_e4d5bf2c12674533b0dc",
    "prd_ps_e228270473ce4c3a9005",
)

MODERN_BRACELETS = (
    "prd_ps_6f5eaca0f520478e8016",
    "prd_ps_2e246edc9aff48aa964c",
    "prd_ps_a243069a547845fdbb05",
    "prd_ps_117efa8e29954f3688ac",
)

MODERN_NECKLACES = (
    "prd_ps_3eae7f7cb88548caa92a",
    "prd_ps_3e18726fce7a4377b089",
    "prd_ps_df2100b394304d669d46",
    "prd_ps_52e6263cbd594e6d904e",
    "prd_ps_ecfa4a6f62f64c849e5d",
)


OCCASION_UPDATES: dict[str, FieldUpdate] = {}

for product_id in TRADITIONAL_EARRINGS:
    OCCASION_UPDATES[product_id] = FieldUpdate(
        (),
        ("birthday", "anniversary", "wedding", "engagement", "diwali", "just-because"),
    )

for product_id in MODERN_EARRINGS:
    OCCASION_UPDATES[product_id] = FieldUpdate((), ("birthday", "anniversary", "just-because"))

for product_id in HEART_JEWELLERY:
    OCCASION_UPDATES[product_id] = FieldUpdate(
        (),
        (
            "birthday",
            "anniversary",
            "proposal",
            "romantic-surprise",
            "valentines-day",
            "just-because",
        ),
    )

for product_id in FLORAL_JEWELLERY:
    OCCASION_UPDATES[product_id] = FieldUpdate(
        (),
        (
            "birthday",
            "anniversary",
            "romantic-surprise",
            "valentines-day",
            "mothers-day",
            "just-because",
        ),
    )

for product_id in EVERYDAY_RINGS:
    OCCASION_UPDATES[product_id] = FieldUpdate((), ("birthday", "anniversary", "just-because"))

for product_id in MODERN_BRACELETS:
    OCCASION_UPDATES[product_id] = FieldUpdate(
        (), ("birthday", "anniversary", "rakhi", "just-because")
    )

OCCASION_UPDATES["prd_ps_88e7599b869c445d8c6d"] = FieldUpdate(
    (),
    (
        "birthday",
        "anniversary",
        "wedding",
        "engagement",
        "diwali",
        "rakhi",
        "just-because",
    ),
)

for product_id in MODERN_NECKLACES:
    desired = ["birthday", "anniversary", "just-because"]
    if product_id == "prd_ps_3eae7f7cb88548caa92a":
        desired.insert(2, "congratulations")
    OCCASION_UPDATES[product_id] = FieldUpdate((), tuple(desired))

OCCASION_UPDATES.update(
    {
        "prd_gv_s_breeze": FieldUpdate(
            ("birthday", "diwali", "just-because"),
            (
                "birthday",
                "diwali",
                "housewarming",
                "thank-you",
                "corporate",
                "just-because",
            ),
        ),
        "prd_gv_s_laven": FieldUpdate(
            ("birthday", "diwali", "just-because"),
            (
                "birthday",
                "mothers-day",
                "diwali",
                "housewarming",
                "get-well-soon",
                "just-because",
            ),
        ),
        "prd_gv_s_njasmine": FieldUpdate(
            ("birthday", "diwali", "just-because"),
            (
                "birthday",
                "anniversary",
                "romantic-surprise",
                "diwali",
                "housewarming",
                "just-because",
            ),
        ),
        "prd_gv_s_rose": FieldUpdate(
            ("birthday", "anniversary", "diwali", "just-because"),
            (
                "birthday",
                "anniversary",
                "romantic-surprise",
                "valentines-day",
                "diwali",
                "housewarming",
                "just-because",
            ),
        ),
        "prd_1784268711_86d686e6": FieldUpdate(
            (
                "anniversary",
                "wedding",
                "engagement",
                "romantic-surprise",
                "valentines-day",
            ),
            (
                "birthday",
                "anniversary",
                "wedding",
                "engagement",
                "proposal",
                "romantic-surprise",
                "valentines-day",
                "just-because",
            ),
        ),
        "prd_1784268770_7332d961": FieldUpdate(
            ("rakhi", "premium-gifting", "birthday"),
            ("rakhi", "bhai-dooj", "premium-gifting"),
        ),
        "prd_1783338172_1696145c": FieldUpdate(
            ("premium-gifting", "corporate", "diwali", "wedding", "just-because"),
            ("wedding", "diwali", "housewarming", "corporate", "premium-gifting"),
        ),
        "prd_1783265395_24c483f5": FieldUpdate(
            ("anniversary", "birthday", "diwali", "wedding", "housewarming"),
            (
                "birthday",
                "anniversary",
                "wedding",
                "engagement",
                "diwali",
                "housewarming",
                "premium-gifting",
            ),
        ),
        "prd_1783268600_6948e770": FieldUpdate(
            ("diwali", "just-because"),
            (
                "wedding",
                "diwali",
                "housewarming",
                "corporate",
                "premium-gifting",
                "just-because",
            ),
        ),
        "prd_1783253331_b21ba711": FieldUpdate(
            ("birthday", "anniversary", "diwali", "wedding", "premium-gifting"),
            (
                "birthday",
                "anniversary",
                "wedding",
                "engagement",
                "diwali",
                "housewarming",
                "premium-gifting",
            ),
        ),
        "prd_1783521966_14ee036d": FieldUpdate(
            (
                "anniversary",
                "wedding",
                "engagement",
                "valentines-day",
                "romantic-surprise",
                "mothers-day",
                "fathers-day",
                "proposal",
                "birthday",
                "diwali",
                "rakhi",
                "premium-gifting",
                "get-well-soon",
                "farewell",
                "congratulations",
                "new-year",
                "bhai-dooj",
                "just-because",
                "corporate",
                "thank-you",
            ),
            (
                "birthday",
                "anniversary",
                "wedding",
                "engagement",
                "romantic-surprise",
                "valentines-day",
                "mothers-day",
                "diwali",
                "baby-shower",
                "thank-you",
                "corporate",
                "premium-gifting",
                "just-because",
            ),
        ),
        "prd_1783338163_0cc56810": FieldUpdate(
            (
                "anniversary",
                "wedding",
                "engagement",
                "birthday",
                "proposal",
                "romantic-surprise",
                "valentines-day",
                "mothers-day",
                "fathers-day",
                "diwali",
                "rakhi",
                "bhai-dooj",
                "eid",
                "christmas",
                "new-year",
                "baby-shower",
                "congratulations",
                "thank-you",
                "corporate",
                "premium-gifting",
                "just-because",
            ),
            (
                "birthday",
                "anniversary",
                "wedding",
                "diwali",
                "rakhi",
                "thank-you",
                "corporate",
                "premium-gifting",
                "just-because",
            ),
        ),
    }
)


def _jewellery_description(lead: str) -> str:
    return (
        f"{lead} "
        "It is designed as a versatile fashion accessory for casual, work or "
        "occasion styling and makes a thoughtful gift for birthdays and personal "
        "celebrations. Refer to the listing images for the exact finish, scale "
        "and proportions."
    )


# The leads use visible design details from the existing production titles and
# images. They deliberately avoid unrecorded claims about material, size,
# plating, weight, skin sensitivity or durability.
JEWELLERY_DESCRIPTION_SPECS = {
    "prd_ps_a4eb10f333e34ace93c1": (
        "Beach celestial ring",
        "A beach-inspired celestial fashion ring with a playful motif that adds "
        "an easy coastal accent to everyday outfits.",
    ),
    "prd_ps_1e2f4d9c3ad443b68f58": (
        "Curved leaves celestial ring",
        "A celestial fashion ring shaped with curved leaf details for a delicate, "
        "nature-inspired look.",
    ),
    "prd_ps_ecc5a3c9a3584f68a10b": (
        "Dainty Hollow Heart Pendant Necklace",
        "A dainty pendant necklace centred on an open heart motif for a light, romantic accent.",
    ),
    "prd_ps_2a448fe8d5d64282847c": (
        "Delicate Enamel Rose Necklace",
        "A delicate necklace featuring an enamel rose motif that brings a floral, "
        "romantic touch to simple outfits.",
    ),
    "prd_ps_eda4ed236f404c45b1b1": (
        "Edge ring",
        "A contemporary fashion ring with a clean-edged silhouette for a simple, modern statement.",
    ),
    "prd_ps_efa0f504313b43b8885d": (
        "Every day ring",
        "A simple fashion ring intended as an easy finishing detail for everyday styling.",
    ),
    "prd_ps_1689746a749441fe9a0f": (
        "Everyday celestial ring",
        "An everyday fashion ring with celestial-inspired detailing for a subtle "
        "and playful accent.",
    ),
    "prd_ps_cfa0b882a30548439388": (
        "Eye ring",
        "A fashion ring built around an eye motif for a distinctive symbolic accent.",
    ),
    "prd_ps_72dc4efe2ae344caadfc": (
        "Fashion ring",
        "A contemporary fashion ring that adds a neat statement detail without "
        "overpowering the rest of the look.",
    ),
    "prd_ps_3bd2f098d60e432e81e8": (
        "Flower celestial ring",
        "A celestial fashion ring with a floral motif for a soft, nature-inspired accent.",
    ),
    "prd_ps_d5c60badede1454aa7e2": (
        "Flower celestial ring",
        "A flower-motif celestial ring with a distinct sculpted arrangement shown "
        "in this listing's product image.",
    ),
    "prd_ps_5f48717cb00745b8bdf1": (
        "Galaxy celestial ring",
        "A galaxy-inspired celestial fashion ring that adds a playful night-sky "
        "theme to everyday styling.",
    ),
    "prd_ps_6f5eaca0f520478e8016": (
        "Geometric Fashion Bracelet",
        "A modern fashion bracelet with geometric detailing for a clean, contemporary statement.",
    ),
    "prd_ps_16cfd7df882244bbb8b3": (
        "Gold plated stainless steel stud earrings",
        "Gold-plated stainless-steel stud earrings with a compact silhouette "
        "suited to understated everyday styling.",
    ),
    "prd_ps_fe0f74a38b7b401aa5b7": (
        "Gold plated stainless steel sun shaped earrings",
        "Gold-plated stainless-steel earrings shaped around a radiant sun motif "
        "for a bright statement detail.",
    ),
    "prd_ps_4a8a957c987b4220b879": (
        "Gold teardrop pearl stud earrings",
        "Gold-tone teardrop stud earrings accented with pearls for an elegant occasion-ready look.",
    ),
    "prd_ps_3796c422b60345568841": (
        "Heart sequence ring",
        "A romantic fashion ring arranged with a sequence of heart motifs for a playful statement.",
    ),
    "prd_ps_2328567897e64e8e86ca": (
        "Heart shaped stud earrings",
        "Heart-shaped stud earrings with a compact romantic motif for easy daily "
        "or occasion styling.",
    ),
    "prd_ps_254da2fd18e346a6a2cc": (
        "Hoops earrings",
        "Puffy heart-shaped hoop earrings with a smooth, rounded gold-tone profile "
        "and hinged closure visible in the listing images.",
    ),
    "prd_ps_3aaacef09ac34934a4e2": (
        "Hoops earrings",
        "Pointed teardrop hoop earrings with an open V-shaped gold-tone silhouette "
        "for a clean geometric statement.",
    ),
    "prd_ps_6907b22128f0415c8827": (
        "Hoops earrings",
        "Sculptural gold-tone hoop earrings with a softly twisted, rounded profile "
        "and fluid contemporary shape.",
    ),
    "prd_ps_7c141a4ced2840bb9b7a": (
        "Hoops earrings",
        "Hammered heart-shaped hoop earrings in a gold-tone finish, combining a "
        "romantic outline with textured detailing.",
    ),
    "prd_ps_87ed07994335442c9304": (
        "Hoops earrings",
        "Compact oval hoop earrings with a hammered gold-tone surface and a softly "
        "tapered profile.",
    ),
    "prd_ps_8c8df5d7540c415dab55": (
        "Hoops earrings",
        "Open heart-outline earrings in a polished gold-tone finish with a clean, "
        "minimal curved profile.",
    ),
    "prd_ps_e0494d200bfc486aac0f": (
        "Hoops earrings",
        "Chunky crescent-shaped hoop earrings with a broad polished gold-tone "
        "profile for a bold modern look.",
    ),
    "prd_ps_f0df411dd6654ab3b2b2": (
        "Hoops earrings",
        "Geometric trapezoid hoop earrings with a broad gold-tone frame and an "
        "angular open centre.",
    ),
    "prd_ps_f6c381768c914515ac6b": (
        "Hoops earrings",
        "Pointed leaf-shaped hoop earrings with diagonal textured detailing in a gold-tone finish.",
    ),
    "prd_ps_fadc7308302644fab56f": (
        "Hoops earrings",
        "Statement hoop earrings formed from a sequence of rounded heart motifs "
        "in a gold-tone finish.",
    ),
    "prd_ps_97fcc7d14f454904bf85": (
        "Korean Earrings",
        "Contemporary Korean-style earrings designed to add a polished fashion "
        "accent to simple outfits.",
    ),
    "prd_ps_9687b253f08a4cb68efa": (
        "Leaf cascade ring",
        "A nature-inspired fashion ring with cascading leaf details that create a "
        "flowing statement.",
    ),
    "prd_ps_4a3ba27f35f647e4a073": (
        "Leaf celestial ring",
        "A celestial fashion ring featuring a leaf motif for a subtle botanical accent.",
    ),
    "prd_ps_3eae7f7cb88548caa92a": (
        "Lucky Charm Horseshoe Necklace",
        "A charm necklace centred on a horseshoe motif, making it a symbolic gift "
        "for luck and new beginnings.",
    ),
    "prd_ps_3e18726fce7a4377b089": (
        "Mairah Ocean Inspired Starfish Pendant Chain",
        "An ocean-inspired pendant chain featuring a starfish motif for a playful "
        "coastal statement.",
    ),
    "prd_ps_dca0581055d44d2e85ca": (
        "Minimal celestial ring",
        "A minimal celestial fashion ring with understated detailing for an easy everyday accent.",
    ),
    "prd_ps_7f035fd8f006476fb026": (
        "Minimal leaf ring",
        "A minimal fashion ring shaped with a leaf motif for a delicate botanical detail.",
    ),
    "prd_ps_e4d5bf2c12674533b0dc": (
        "Moon star celestial ring",
        "A celestial fashion ring featuring moon-and-star motifs for a playful night-sky theme.",
    ),
    "prd_ps_e228270473ce4c3a9005": (
        "Multilayer ring",
        "A multilayer fashion ring with stacked visual lines for a fuller statement look.",
    ),
    "prd_ps_df2100b394304d669d46": (
        "Paper Clip Necklace",
        "A contemporary necklace built around paperclip-style links for a clean, modern look.",
    ),
    "prd_ps_64ca9e7453d449c38d62": (
        "Paperclip Chain Heart Charm Necklace",
        "A paperclip-style chain finished with a heart charm for a modern romantic accent.",
    ),
    "prd_ps_2e246edc9aff48aa964c": (
        "Pearl fashion bracelet",
        "A fashion bracelet accented with pearls for a classic detail that can "
        "move between everyday and occasion looks.",
    ),
    "prd_ps_8a846d1e0ff84e5593f3": (
        "Pretty heart necklace",
        "A heart-motif necklace designed as a simple romantic accent for everyday "
        "or celebration styling.",
    ),
    "prd_ps_52e6263cbd594e6d904e": (
        "Retro Long Minimalist Pendant Chain Necklace",
        "A long minimalist pendant-chain necklace with a retro-inspired silhouette "
        "for easy layered styling.",
    ),
    "prd_ps_88e7599b869c445d8c6d": (
        "Rhinestone Bangle kada",
        "A kada-style bangle accented with rhinestones for a sparkling festive or "
        "occasion-ready statement.",
    ),
    "prd_ps_3a3b68dfe7a94dac9c35": (
        "Shiny triple hoop earrings",
        "Shiny triple-hoop earrings that create a layered look while retaining a "
        "compact modern silhouette.",
    ),
    "prd_ps_ecfa4a6f62f64c849e5d": (
        "Solyn Curved Arc Necklace",
        "A contemporary necklace shaped around a smooth curved-arc motif for a "
        "minimal architectural accent.",
    ),
    "prd_ps_e634d912a93a4528b3b5": (
        "Soulmate Heart Necklace",
        "A heart-motif necklace designed as a romantic gift for a partner or someone special.",
    ),
    "prd_ps_a243069a547845fdbb05": (
        "Sparkling Square Link Bracelate",
        "A sparkling bracelet built from square-link details for a structured statement look.",
    ),
    "prd_ps_cc26d558044546e096bc": (
        "Sparqle Delicate Gold Heart Pendant Necklace",
        "A delicate gold-tone heart pendant necklace with a subtle sparkling "
        "accent for romantic styling.",
    ),
    "prd_ps_117efa8e29954f3688ac": (
        "Stretch Crystal Rhinestone Bracelet",
        "A stretch bracelet set with crystal rhinestones and offered in Beige, "
        "Black, Golden and Silver colour options.",
    ),
    "prd_ps_6e2d30c0d62643d98b9c": (
        "Sunflower ring",
        "A cheerful fashion ring featuring a sunflower motif for a bright floral accent.",
    ),
    "prd_ps_2e06df229a5f49eeb67d": (
        "korean earrings",
        "Korean-style fashion earrings with a contemporary silhouette intended to "
        "lift simple everyday looks.",
    ),
}


DESCRIPTION_UPDATES = {
    product_id: FieldUpdate(expected, _jewellery_description(lead))
    for product_id, (expected, lead) in JEWELLERY_DESCRIPTION_SPECS.items()
}

DESCRIPTION_UPDATES.update(
    {
        "prd_1783338163_0cc56810": FieldUpdate(
            (
                "<h2>A made-to-order gift curated with your signature products "
                "and finished with personalisation.</h2><p></p>"
            ),
            (
                "A made-to-order festive hamper arranged in a premium wooden "
                "basket with decorative flowers, soft pastel ribbons and selected "
                "wellness products. The gift includes a personalised note and "
                "protective packaging, while the final product selection can be "
                "curated for the recipient and occasion."
            ),
        ),
        "prd_1782475741_891f9818": FieldUpdate(
            (
                "More than flowers, a bouquet of warmth and fragrance crafted to "
                "make every celebration unforgettable."
            ),
            (
                "A decorative candle bouquet arranged as a lasting alternative to "
                "fresh flowers and designed for birthdays, anniversaries and "
                "thoughtful surprises. Flower styles, colours, overall design and "
                "fragrance can be customised, making the finished bouquet suitable "
                "for personal as well as bulk gifting."
            ),
        ),
        "prd_1782475742_b52cb41d": FieldUpdate(
            (
                "Small in size, big on elegance - custom-crafted tin candles that "
                "make every gift memorable."
            ),
            (
                "A custom candle presented in a compact tin for convenient gifting "
                "and display. Floral decoration, colours, design details and "
                "fragrance can be customised for the occasion, making it suitable "
                "for birthdays, return gifts, corporate orders and personalised "
                "gift sets."
            ),
        ),
        "prd_1782475743_c0ba4893": FieldUpdate(
            (
                "A charming blend of love and elegance, beautifully crafted in a "
                "glass jar to make every moment unforgettable."
            ),
            (
                "A custom candle presented in a reusable tin and designed for gift "
                "sets, party favours and personal celebrations. The colour can be "
                "customised and coordinated combo sets are available, allowing the "
                "final presentation to be matched to the recipient or event."
            ),
        ),
        "prd_1782475742_d589d0b4": FieldUpdate(
            (
                "A graceful blend of blooming beauty and timeless elegance, "
                "designed to brighten your space with warmth and charm."
            ),
            (
                "A decorative peony flower candle presented with a ceramic bubble "
                "tray, combining a floral centrepiece with a reusable display "
                "surface. Colour customisation is available, making it suitable "
                "for birthdays, anniversaries, table décor and thoughtful gift "
                "sets."
            ),
        ),
        "prd_1782475741_4b0bf0f0": FieldUpdate(
            (
                "Fill your space with elegance and captivating fragrances, "
                "crafted to create moments of warmth and serenity."
            ),
            (
                "A fragrance jar candle designed to add a warm scented accent to "
                "bedrooms, living spaces and gift arrangements. Custom colour "
                "options are available so the candle can be coordinated with an "
                "event, décor theme or hamper presentation."
            ),
        ),
        "prd_1782475742_262c0e73": FieldUpdate(
            (
                "A bouquet that never fades where luxury, fragrance, and elegance "
                "come together to create unforgettable memories."
            ),
            (
                "A premium bouquet-style candle arrangement designed as a "
                "decorative, fragrant gift that does not rely on fresh flowers. "
                "Flower styles, colours, design details and fragrance can be "
                "customised for birthdays, anniversaries, romantic surprises and "
                "premium gift orders."
            ),
        ),
        "prd_1782475743_52b74e40": FieldUpdate(
            (
                "A symbol of love, warmth, and affection crafted to make every "
                "special moment even more memorable."
            ),
            (
                "A decorative candle combining rose, heart and teddy-bear motifs "
                "for birthdays, anniversaries and romantic gifting. Custom colours "
                "and coordinated combo sets are available, allowing the finished "
                "gift to be matched to the recipient or celebration theme."
            ),
        ),
    }
)


TARGET_PRODUCT_IDS = frozenset(
    set(CATEGORY_UPDATES) | set(OCCASION_UPDATES) | set(DESCRIPTION_UPDATES)
)


def _normalise_occasions(value: Any) -> tuple[str, ...]:
    return tuple(sorted(str(item) for item in (value or [])))


def transition_needed(
    current: Any,
    update: FieldUpdate,
    *,
    label: str,
    normalise=lambda value: value,
) -> bool:
    """Return whether a guarded field needs changing, or raise on drift."""
    current_value = normalise(current)
    expected_value = normalise(update.expected)
    desired_value = normalise(update.desired)

    if current_value == desired_value:
        return False
    if current_value != expected_value:
        raise RuntimeError(
            f"{label} drifted: found {current!r}; "
            f"expected audited {update.expected!r} or final {update.desired!r}"
        )
    return True


def _validate_static_specs() -> None:
    missing_names = TARGET_PRODUCT_IDS - set(PRODUCT_NAMES)
    unused_names = set(PRODUCT_NAMES) - TARGET_PRODUCT_IDS
    if missing_names or unused_names:
        raise RuntimeError(
            "Static product-name coverage is inconsistent: "
            f"missing={sorted(missing_names)!r}, unused={sorted(unused_names)!r}"
        )

    desired_occasions = {
        occasion for update in OCCASION_UPDATES.values() for occasion in update.desired
    }
    unknown_occasions = desired_occasions - CANONICAL_OCCASION_IDS
    if unknown_occasions:
        raise RuntimeError(
            f"Static corrections contain unknown occasions: {sorted(unknown_occasions)!r}"
        )


_validate_static_specs()


async def apply_catalog_corrections(db: AsyncSession) -> CorrectionSummary:
    """Validate and apply corrections inside the caller's transaction."""
    products = (
        (
            await db.execute(
                select(Product)
                .where(Product.id.in_(TARGET_PRODUCT_IDS))
                .order_by(Product.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    products_by_id = {product.id: product for product in products}

    errors: list[str] = []
    missing_products = TARGET_PRODUCT_IDS - set(products_by_id)
    if missing_products:
        errors.append(f"missing target products: {sorted(missing_products)!r}")

    existing_categories = (
        (await db.execute(select(ProductCategory).order_by(ProductCategory.id))).scalars().all()
    )
    categories_by_id = {category.id: category for category in existing_categories}
    category_names = {category.name for category in existing_categories}

    categories_to_create: list[CategorySpec] = []
    for spec in NEW_CATEGORIES:
        existing = categories_by_id.get(spec.id)
        if existing is None:
            categories_to_create.append(spec)
            category_names.add(spec.name)
            continue
        if existing.name != spec.name or existing.sort_order != spec.sort_order:
            errors.append(
                f"taxonomy category {spec.id!r} drifted: "
                f"found name={existing.name!r}, sort_order={existing.sort_order!r}; "
                f"expected name={spec.name!r}, sort_order={spec.sort_order!r}"
            )

    desired_category_names = {str(update.desired) for update in CATEGORY_UPDATES.values()}
    missing_category_names = desired_category_names - category_names
    if missing_category_names:
        errors.append(f"desired category names are unavailable: {sorted(missing_category_names)!r}")

    production_occasion_ids = set(
        (await db.execute(select(GiftingOccasion.id).order_by(GiftingOccasion.id))).scalars().all()
    )
    desired_occasion_ids = {
        occasion for update in OCCASION_UPDATES.values() for occasion in update.desired
    }
    missing_occasion_ids = desired_occasion_ids - production_occasion_ids
    if missing_occasion_ids:
        errors.append(f"desired occasion slugs are unavailable: {sorted(missing_occasion_ids)!r}")

    planned_categories: set[str] = set()
    planned_occasions: set[str] = set()
    planned_descriptions: set[str] = set()

    for product_id, product in products_by_id.items():
        expected_name = PRODUCT_NAMES[product_id]
        if (product.name or "").strip() != expected_name.strip():
            errors.append(
                f"{product_id} name drifted: found {product.name!r}; expected {expected_name!r}"
            )
            continue

        try:
            category_update = CATEGORY_UPDATES.get(product_id)
            if category_update and transition_needed(
                product.category,
                category_update,
                label=f"{product_id}.category",
            ):
                planned_categories.add(product_id)

            occasion_update = OCCASION_UPDATES.get(product_id)
            if occasion_update and transition_needed(
                product.occasions,
                occasion_update,
                label=f"{product_id}.occasions",
                normalise=_normalise_occasions,
            ):
                planned_occasions.add(product_id)

            description_update = DESCRIPTION_UPDATES.get(product_id)
            if description_update and transition_needed(
                product.description,
                description_update,
                label=f"{product_id}.description",
            ):
                planned_descriptions.add(product_id)
        except RuntimeError as exc:
            errors.append(str(exc))

    if errors:
        raise RuntimeError(
            "Refusing to modify the catalogue because production drift was found:\n- "
            + "\n- ".join(errors)
        )

    for spec in categories_to_create:
        db.add(
            ProductCategory(
                id=spec.id,
                name=spec.name,
                sort_order=spec.sort_order,
            )
        )
        print(f"CREATE CATEGORY {spec.id}: {spec.name!r} (sort_order={spec.sort_order})")

    for product_id in sorted(planned_categories):
        product = products_by_id[product_id]
        update = CATEGORY_UPDATES[product_id]
        product.category = update.desired
        print(f"CATEGORY {product_id} ({product.name}): {update.expected!r} -> {update.desired!r}")

    for product_id in sorted(planned_occasions):
        product = products_by_id[product_id]
        update = OCCASION_UPDATES[product_id]
        product.occasions = list(update.desired)
        print(
            f"OCCASIONS {product_id} ({product.name}): "
            f"{list(update.expected)!r} -> {list(update.desired)!r}"
        )

    for product_id in sorted(planned_descriptions):
        product = products_by_id[product_id]
        update = DESCRIPTION_UPDATES[product_id]
        product.description = update.desired
        print(
            f"DESCRIPTION {product_id} ({product.name}): "
            f"{len(str(update.expected))} -> {len(str(update.desired))} chars"
        )

    await db.flush()
    return CorrectionSummary(
        categories_created=len(categories_to_create),
        product_categories_changed=len(planned_categories),
        occasions_changed=len(planned_occasions),
        descriptions_changed=len(planned_descriptions),
    )


async def invalidate_catalog_caches() -> None:
    """Best-effort invalidation after a committed direct database correction."""
    redis = get_redis()
    if redis is None:
        print("CACHE: Redis is not configured; normal TTLs will refresh catalogue data")
        return
    try:
        deleted = await redis.delete(PRODUCTS_CACHE_KEY, TAXONOMY_CACHE_KEY)
        print(f"CACHE: invalidated product and taxonomy keys ({deleted} key(s) removed)")
    except Exception as exc:
        print(
            "CACHE WARNING: commit succeeded but Redis invalidation failed; "
            f"normal TTLs will refresh the catalogue ({exc})"
        )


async def correct_catalog(
    *,
    commit: bool,
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    invalidate_cache: bool = True,
) -> CorrectionSummary:
    async with session_factory() as db:
        summary = await apply_catalog_corrections(db)
        print(
            "SUMMARY: "
            f"taxonomy_created={summary.categories_created}, "
            f"categories_changed={summary.product_categories_changed}, "
            f"occasions_changed={summary.occasions_changed}, "
            f"descriptions_changed={summary.descriptions_changed}, "
            f"total={summary.total_changes}"
        )

        if commit:
            await db.commit()
            print("COMMITTED")
        else:
            await db.rollback()
            print("DRY RUN ONLY - transaction rolled back; pass --commit to persist")

    if commit and invalidate_cache and summary.total_changes:
        await invalidate_catalog_caches()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply the reviewed 2026-07-25 production catalogue corrections."
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Persist changes. Without this flag the transaction is rolled back.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(correct_catalog(commit=args.commit))


if __name__ == "__main__":
    main()
