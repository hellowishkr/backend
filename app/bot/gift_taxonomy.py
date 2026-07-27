"""Vibe taxonomy — maps user-described "vibes" to brand allowlists across sources.

Hand-curated for Indian consumer context. See documentation/issues/007-gift-vibe-taxonomy.md
for the full taxonomy design and reasoning.

Editing rules:
- Vibe IDs are immutable once shipped (stored on celebrant_occasion.preferences)
- Brand names are matched case-insensitively against Hubble brand titles
- New partners → add brand name to existing vibe(s); new vibe only when uncovered
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────
# VIBE → HUBBLE BRAND TITLES
#
# Match is case-insensitive substring on brand `title` from Hubble catalogue.
# Example: "amazon" matches "Amazon Pay", "Amazon Fashion", etc.
# ─────────────────────────────────────────────────────────────────────

VIBE_TO_HUBBLE_BRANDS: dict[str, list[str]] = {
    # Wellness & Fitness
    "gym_fitness": ["cult.fit", "cultfit", "decathlon", "puma", "adidas", "nike", "hrx"],
    "yoga_meditation": ["cult.fit", "cultfit"],
    "sports_outdoor": ["decathlon", "adidas", "nike", "wildcraft", "puma"],
    "home_workout": ["decathlon", "cult.fit", "cultfit"],
    "wellness_recovery": ["lakme salon", "vlcc", "naturals"],
    "nutrition_supplements": ["healthkart"],

    # Food & Beverage
    "coffee_lover": ["starbucks", "blue tokai", "third wave", "ccd", "cafe coffee day"],
    "tea_chai": ["chaayos", "vahdam"],
    "food_delivery": ["swiggy", "zomato"],
    "gourmet_chocolate": ["smoor", "fabelle", "lindt", "cadbury"],
    "bakery_cake": ["theobroma", "monginis", "ferns n petals", "fnp"],
    "mithai_traditional": ["haldiram", "bikanervala"],
    "healthy_snacks": ["the whole truth", "open secret"],
    "gourmet_hampers": ["fnp", "ferns n petals", "theobroma"],
    "cooking_culinary": ["wonderchef", "borosil", "prestige"],

    # Fashion & Style
    "gen_z_streetwear": ["bewakoof", "snitch", "the souled store", "souled store", "h&m", "myntra"],
    "ethnic_fusion": ["westside", "fabindia", "biba", "w for woman", "aurelia"],
    "premium_western": ["marks & spencer", "tommy hilfiger", "allen solly", "van heusen"],
    "luxury_fashion": ["tata cliq luxury", "ajio luxe"],
    "sneakerhead": ["nike", "adidas", "puma"],
    "bags_wallets": ["da milano", "hidesign", "caprese", "baggit"],
    "watches_accessories": ["titan", "fastrack", "daniel wellington", "helios"],
    "ethnic_wear_occasion": ["manyavar", "soch"],

    # Beauty & Grooming
    "skincare_modern": ["nykaa", "myglamm", "minimalist"],
    "makeup_premium": ["mac", "sephora", "smashbox", "huda beauty"],
    "ayurvedic_natural": ["forest essentials", "kama ayurveda", "biotique", "just herbs"],
    "mens_grooming": ["the man company", "beardo", "ustraa", "bombay shaving"],
    "perfume_fragrance": ["bella vita", "ajmal", "engage", "skinn"],
    "haircare_specialist": ["mamaearth", "wow", "plum", "pilgrim"],
    "salon_spa_voucher": ["lakme salon", "naturals", "looks salon"],

    # Tech & Gadgets
    "mass_electronics": ["croma", "reliance digital", "vijay sales", "amazon"],
    "premium_apple": ["croma", "reliance digital"],
    "audio_lover": ["boat", "boAt", "jbl", "croma"],
    "gaming_console": ["steam", "playstation", "xbox"],
    # Mobile/BGMI/Free Fire crowd — in-game UC isn't on Hubble yet, so we
    # route to electronics + Google Play (Play Store top-up funds UC in
    # most cases) + accessory-friendly marketplace vouchers.
    "mobile_gaming": ["google play", "amazon", "flipkart", "croma", "reliance digital", "boat", "boAt", "jbl"],
    "smart_home": ["amazon", "croma"],
    "computing_productivity": ["croma", "reliance digital", "amazon"],
    "phone_accessories": ["boat", "boAt", "mivi", "portronics"],

    # Home & Décor
    "home_decor_modern": ["pepperfry", "urban ladder", "home centre"],
    "kitchen_essentials": ["wonderchef", "borosil", "prestige"],
    "plants_garden": [],  # vendor-only category (Ugaoo not on Hubble)
    "bedding_linens": ["spaces", "d'decor"],
    "candles_fragrance": ["bath & body works"],
    "wall_art": [],

    # Books & Learning
    "book_lover": ["amazon", "kindle", "crossword"],
    "kids_learning": ["hamleys", "firstcry"],
    "online_courses": ["coursera", "udemy", "unacademy"],
    "audiobooks_podcasts": ["audible", "kindle"],

    # Entertainment & Experiences
    "movies_ott": ["bookmyshow", "netflix", "amazon prime", "jiohotstar", "hotstar"],
    "live_events": ["bookmyshow", "district"],
    "music_streaming": ["spotify", "jiosaavn", "youtube premium"],
    "private_theatre": [],  # vendor-only
    "board_games": [],
    "escape_rooms_arcade": [],

    # Travel & Adventure
    "travel_voucher": ["makemytrip", "yatra", "cleartrip", "goibibo"],
    "adventure_experiences": ["thrillophilia"],
    "weekend_getaway": ["oyo", "treebo"],
    "ride_hailing": ["uber", "ola"],
    "luggage_travel_gear": ["american tourister", "vip", "wildcraft"],
    "staycation_premium": [],

    # Personalized & Sentimental
    "personalized_keepsake": [],  # vendor-only (Confetti, Good Road)
    "greeting_cards": ["archies"],
    "photo_album_print": [],
    "flowers_classic": ["fnp", "ferns n petals", "floweraura"],
    "fresh_flowers_local": [],  # vendor-only (Pune florists)
    "handwritten_letter": [],
    "memory_engraving": [],

    # Jewellery
    "silver_everyday": ["caratlane"],  # vendor side has GIVA, Tarinika
    "fashion_jewellery": ["caratlane"],
    "fine_gold_diamond": ["tanishq", "caratlane", "bluestone"],
    "bridal_jewellery": ["tanishq"],

    # Spiritual & Cultural
    "pooja_essentials": [],
    "spiritual_books_courses": ["audible"],
    "meditation_retreat": [],

    # Pets
    "pet_parent": ["heads up for tails", "supertails"],
    "pet_treats_food": [],

    # Practical / Subscriptions / Recharges
    "groceries": ["bigbasket", "blinkit", "zepto", "amazon fresh"],
    "fuel_voucher": ["indianoil", "hpcl"],
    "mobile_recharge": [],  # routed via Xoxoday per partner_strategy memory
    "cab_vouchers": ["uber", "ola"],
    "cleaning_services": ["urban company"],
    "ott_bundle": ["netflix", "amazon prime", "hotstar", "jiohotstar"],
}


# ─────────────────────────────────────────────────────────────────────
# VIBE → VENDOR CATEGORIES
#
# Matches Vendor.category (free string in DB). Aligns with categories
# already used in alembic migration d7c94a1f2e58: private_theatre, cake,
# jewellery, gift_card. Add new categories as partners onboard.
# ─────────────────────────────────────────────────────────────────────

VIBE_TO_VENDOR_CATEGORIES: dict[str, list[str]] = {
    "bakery_cake": ["cake", "bakery"],
    "private_theatre": ["private_theatre"],
    "fresh_flowers_local": ["florist", "flowers"],
    "personalized_keepsake": ["personalized_gift", "personalised_gift"],
    "memory_engraving": ["personalized_gift", "personalised_gift"],
    "silver_everyday": ["jewellery"],
    "fashion_jewellery": ["jewellery"],
    "fine_gold_diamond": ["jewellery"],
    "bridal_jewellery": ["jewellery"],
    "skincare_modern": ["skincare", "wellness"],
    "ayurvedic_natural": ["skincare", "wellness"],
    "salon_spa_voucher": ["salon", "spa"],
    "wellness_recovery": ["spa", "wellness"],
    "gourmet_chocolate": ["chocolates"],
    "mithai_traditional": ["mithai", "sweets"],
    "bags_wallets": ["bags", "accessories"],
    "watches_accessories": ["accessories"],
    "plants_garden": ["plants", "nursery"],
    "wall_art": ["art"],
    "board_games": ["games"],
    "escape_rooms_arcade": ["experience"],
    "weekend_getaway": ["resort", "stay"],
    "staycation_premium": ["resort", "stay"],
    "adventure_experiences": ["experience"],
    "pooja_essentials": ["religious"],
    "pet_parent": ["pet"],
    "pet_treats_food": ["pet"],
    "ethnic_wear_occasion": ["ethnic_wear"],
}


# ─────────────────────────────────────────────────────────────────────
# VIBE METADATA — for ranking and prompt hints
# ─────────────────────────────────────────────────────────────────────

# Vibes appropriate for each occasion type. Used to infer vibes if user
# names an occasion but no explicit vibe.
OCCASION_DEFAULT_VIBES: dict[str, list[str]] = {
    "birthday": ["food_delivery", "movies_ott", "gen_z_streetwear", "audio_lover", "mobile_gaming"],
    "anniversary": ["flowers_classic", "gourmet_chocolate", "jewellery_fashion", "private_theatre", "weekend_getaway"],
    "wedding": ["bridal_jewellery", "ethnic_wear_occasion", "home_decor_modern", "kitchen_essentials"],
    "housewarming": ["home_decor_modern", "kitchen_essentials", "plants_garden", "candles_fragrance"],
    "baby_shower": ["kids_learning"],
    "diwali": ["gourmet_hampers", "mithai_traditional", "gourmet_chocolate", "candles_fragrance", "ayurvedic_natural"],
    "rakhi": ["gourmet_chocolate", "mithai_traditional", "watches_accessories", "perfume_fragrance"],
    "valentine": ["flowers_classic", "gourmet_chocolate", "private_theatre", "personalized_keepsake"],
    "mother_day": ["flowers_classic", "ayurvedic_natural", "salon_spa_voucher", "silver_everyday"],
    "father_day": ["watches_accessories", "mens_grooming", "perfume_fragrance", "audio_lover"],
    "promotion": ["luxury_fashion", "watches_accessories", "premium_western"],
    "farewell": ["personalized_keepsake", "memory_engraving", "watches_accessories"],
}


def all_vibe_ids() -> list[str]:
    """Return all known vibe IDs (union of Hubble + Vendor mappings)."""
    return sorted(set(VIBE_TO_HUBBLE_BRANDS) | set(VIBE_TO_VENDOR_CATEGORIES))


def hubble_titles_for_vibes(vibes: list[str]) -> set[str]:
    """Resolve a list of vibes to a flat set of Hubble brand title fragments."""
    out: set[str] = set()
    for v in vibes:
        out.update(t.lower() for t in VIBE_TO_HUBBLE_BRANDS.get(v, []))
    return out


def vendor_categories_for_vibes(vibes: list[str]) -> set[str]:
    """Resolve a list of vibes to vendor categories."""
    out: set[str] = set()
    for v in vibes:
        out.update(c.lower() for c in VIBE_TO_VENDOR_CATEGORIES.get(v, []))
    return out


def default_vibes_for_occasion(occasion_type: str) -> list[str]:
    """Best-guess vibes for a given occasion if user gives no other hint."""
    return OCCASION_DEFAULT_VIBES.get(occasion_type.lower(), [])
