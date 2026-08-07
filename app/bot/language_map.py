"""Single source of truth for deterministic language maps.

These maps are intentionally limited to exact or high-confidence routing and
normalisation. Open-ended understanding stays with the LLM; safety and policy
stay in the adapter/runtime layer.
"""

from __future__ import annotations

import re
from typing import Iterable

AFFIRMATIVE_WORDS = frozenset({
    "y", "yes", "yess", "yeah", "yep", "yup", "ok", "okay", "sure",
    "haan", "haa", "ha", "haanji", "haan ji", "ji", "ji haan",
    "theek", "theek hai", "thik", "thik hai",
    "correct", "right", "👍", "✅",
})

MENU_WORDS = frozenset({"menu", "/menu", "help", "/help"})

MENU_EXACT_PHRASES = frozenset({
    "menu", "/menu", "main menu", "show menu", "show me menu",
    "help", "/help", "show help",
    "show options", "list options",
    "what can you do", "what can u do", "what do you do",
})

SHOW_WISHLIST_PHRASES = frozenset({
    "wishlist", "my wishlist",
    "show wishlist", "show my wishlist",
    "view wishlist", "view my wishlist",
    "see wishlist", "see my wishlist",
    "list wishlist", "list my wishlist",
    "open wishlist", "open my wishlist",
    "wishlist?", "my wishlist?",
})

OCCASION_LIST_EXACT_PHRASES = frozenset({
    "my occasions",
    "show my occasions",
    "show occasions",
    "list my occasions",
    "list occasions",
    "view my occasions",
    "see my occasions",
    "what occasions have i added",
    "what occasions did i add",
    "what occasions have i saved",
    "what occasions did i save",
    "what are all occasions i have told you till now",
    "what are all the occasions i have told you till now",
    "can you recall all the occasions whatever i have told you",
    "can you recall whatever occasions i have told you till now",
    "can u recall whatever occasions i have told you till now",
    "can you recall the occasions i have told you",
    "recall my occasions",
    "recall all my occasions",
    "recall the occasions",
    "recall occasions",
    "i want history of my occasions",
    "i want history of my occasions i have told you",
    "history of my occasions",
    "history of occasions",
    "occasions i have told you",
    "occasions i told you",
    "which birthdays did i save",
    "which anniversaries did i save",
    "which birthdays and anniversaries did i save",
    "which birthdays/anniversaries did i save",
})

SAVE_PHRASES = (
    "add to wishlist",
    "wishlist me",
    "save this",
    "keep this",
    "note this",
    "store this",
    "isko save",
    "ye save",
    "yaad rakh",
    "add this",
    "remember",
    "wishlist",
    "save",
)

# NOTE: `VIBE_KEYWORD_MAP` below is CATALOG SEARCH, not intent detection. It
# maps free text to `gift_taxonomy` vibe ids for product ranking and is used
# only by `services/gift_recommendation.py` and `services/gift_refinement.py`.
# It reads like intent parsing because it is keyed on natural words; it is not.
# Deleting it would silently degrade gift ranking to embeddings alone.
VIBE_KEYWORD_MAP: dict[str, list[str]] = {
    "gym": ["gym_fitness"], "fitness": ["gym_fitness"], "workout": ["gym_fitness", "home_workout"],
    "yoga": ["yoga_meditation"], "meditation": ["yoga_meditation"],
    "coffee": ["coffee_lover"], "tea": ["tea_chai"], "chai": ["tea_chai"],
    "foodie": ["food_delivery", "gourmet_chocolate"], "food": ["food_delivery"],
    "cook": ["kitchen_essentials", "bakery_cake"],
    "chef": ["kitchen_essentials", "gourmet_chocolate"],
    "chocolate": ["gourmet_chocolate"], "cake": ["bakery_cake"],
    "tech": ["mass_electronics", "premium_apple"], "gadget": ["mass_electronics", "audio_lover"],
    "earbud": ["audio_lover"], "headphone": ["audio_lover"],
    "bgmi": ["mobile_gaming", "audio_lover"],
    "pubg": ["mobile_gaming", "audio_lover"],
    "free fire": ["mobile_gaming"], "freefire": ["mobile_gaming"],
    "cod": ["mobile_gaming"], "call of duty": ["mobile_gaming"],
    "uc": ["mobile_gaming"], "in-game": ["mobile_gaming"], "ingame": ["mobile_gaming"],
    "gaming": ["mobile_gaming", "gaming_console", "audio_lover"],
    "gamer": ["mobile_gaming", "gaming_console", "audio_lover"],
    "console": ["gaming_console"], "playstation": ["gaming_console"],
    "xbox": ["gaming_console"], "steam": ["gaming_console"],
    "fashion": ["gen_z_streetwear", "premium_western"], "style": ["gen_z_streetwear"],
    "trendy": ["gen_z_streetwear"], "ethnic": ["ethnic_fusion"],
    "clothes": ["gen_z_streetwear", "premium_western"],
    "clothing": ["gen_z_streetwear", "premium_western"],
    "comfy": ["gen_z_streetwear"], "hoodie": ["gen_z_streetwear"],
    "oversized": ["gen_z_streetwear"], "loungewear": ["gen_z_streetwear"],
    "art supplies": ["wall_art"], "artist": ["wall_art"],
    "drawing": ["wall_art"], "painting": ["wall_art"], "sketch": ["wall_art"],
    "skin": ["skincare_modern"], "skincare": ["skincare_modern"],
    "makeup": ["makeup_premium"], "beauty": ["skincare_modern", "makeup_premium"],
    "natural": ["ayurvedic_natural"], "ayurved": ["ayurvedic_natural"],
    "men": ["mens_grooming"], "grooming": ["mens_grooming"],
    "perfume": ["perfume_fragrance"], "fragrance": ["perfume_fragrance"],
    "book": ["book_lover"], "reader": ["book_lover"], "kindle": ["book_lover"],
    "read": ["book_lover"], "novel": ["book_lover"], "literature": ["book_lover"],
    "movie": ["movies_ott", "private_theatre"], "netflix": ["movies_ott"],
    "music": ["music_streaming"], "concert": ["live_events"],
    "travel": ["travel_voucher"], "adventure": ["adventure_experiences"],
    "flower": ["flowers_classic", "fresh_flowers_local"],
    "jewell": ["fashion_jewellery", "silver_everyday"], "silver": ["silver_everyday"],
    "gold": ["fine_gold_diamond"], "diamond": ["fine_gold_diamond"],
    "personal": ["personalized_keepsake"], "engrav": ["memory_engraving"],
    "kid": ["kids_learning"], "baby": ["kids_learning"],
    "pet": ["pet_parent"], "dog": ["pet_parent"], "cat": ["pet_parent"],
    "plant": ["plants_garden"], "home": ["home_decor_modern"],
    "kitchen": ["kitchen_essentials"], "decor": ["home_decor_modern"],
    "shoe": ["sneakerhead"], "sneaker": ["sneakerhead"],
    "bag": ["bags_wallets"], "watch": ["watches_accessories"],
    "married": ["home_decor_modern", "kitchen_essentials", "personalized_keepsake", "flowers_classic"],
    "wedding": ["home_decor_modern", "kitchen_essentials", "personalized_keepsake"],
    "newborn": ["kids_learning"], "baby shower": ["kids_learning"],
    "housewarming": ["home_decor_modern", "kitchen_essentials", "plants_garden"],
}


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def has_explicit_save_intent(text: str) -> bool:
    lower = (text or "").lower()
    return any(phrase in lower for phrase in SAVE_PHRASES)


# REMOVED: `looks_like_occasion_list_request` and `contains_product_keyword`.
#
# Both inferred intent from substrings and word lists. That is how the message
# "Myra - birthday - 7th Aug" — the user ANSWERING our own question — was read
# as a request to list occasions and answered "I don't have any occasions saved
# for you yet", silently dropping six occasions (2026-08 transcript). No keyword
# table can tell an answer from a question, because the difference is in the
# conversation, not the words.
#
# Intent now belongs to `app/bot/intent_router.py`, which reads recent turns.
# The exact-phrase frozensets above survive for zero-LLM whole-message lookups
# only (see `action_router.try_handle_exact_phrases`) — extend the router's
# prompt, not these lists.


def resolve_vibe_keywords(texts: Iterable[str], known_vibes: set[str]) -> set[str]:
    resolved: set[str] = set()
    for text in texts:
        t = (text or "").strip().lower()
        if not t:
            continue
        if t in known_vibes:
            resolved.add(t)
            continue
        t_norm = t.replace(" ", "_").replace("-", "_")
        if t_norm in known_vibes:
            resolved.add(t_norm)
            continue
        for vibe in known_vibes:
            if t_norm and (t_norm in vibe or vibe in t_norm):
                resolved.add(vibe)
        for keyword, mapped in VIBE_KEYWORD_MAP.items():
            if keyword in t:
                resolved.update(v for v in mapped if v in known_vibes)
    return resolved


def active_gift_context_updates_from_text(text: str) -> dict[str, object]:
    lower = (text or "").lower()
    updates: dict[str, object] = {}
    interest_bits: list[str] = []
    vibe_bits: list[str] = []
    if any(k in lower for k in ("bgmi", "pubg", "free fire", "freefire", "cod mobile")):
        interest_bits.append("BGMI/mobile gaming")
        vibe_bits.append("mobile gaming")
    elif any(k in lower for k in ("gaming", "gamer", "mobile game", "mobile games")):
        interest_bits.append("gaming")
        vibe_bits.append("gaming")
    if any(k in lower for k in ("comfy clothes", "clothes", "clothing", "hoodie", "oversized", "loungewear")):
        interest_bits.append("comfy clothes")
        vibe_bits.append("comfy clothes")
    if any(k in lower for k in ("art supplies", "artist", "drawing", "painting", "sketch")):
        interest_bits.append("art supplies")
        vibe_bits.append("art supplies")
    if interest_bits:
        updates["interest"] = ", ".join(dict.fromkeys(interest_bits))
        updates["recipient_vibe"] = ", ".join(dict.fromkeys(vibe_bits or interest_bits))
        updates["current_step"] = "interest_known"
    if any(k in lower for k in ("headphone", "headphones", "earbud", "earbuds", "earphone", "earphones")):
        updates["gift_category"] = "headphones"
        updates["current_step"] = "category_selected"
    return updates
