"""Hand-written Gemini function declarations — single source of truth.

Every tool Gemini can see has an explicit ``types.FunctionDeclaration`` here.
Nothing is auto-derived from Python signatures. Advantages over ADK's auto
schema:

  * Explicit ``enum`` on constrained string fields (occasion_type, scenario,
    entry_type, …) — Gemini honours enums strictly, which kills a large
    class of invented-argument hallucinations.
  * Correct ``required`` lists — optional fields aren't mentioned, so the
    model doesn't fabricate values for them.
  * Typed ``ARRAY`` / ``OBJECT`` params (no more stringly-typed JSON).
  * Per-parameter descriptions — ADK only gets the function-level docstring.

Execution still happens via the Python wrappers in ``adapter.py``. This file
just describes the contract Gemini sees.
"""

from dataclasses import dataclass, field

from google.genai import types

from app.bot.gift_taxonomy import all_vibe_ids

Type = types.Type
Schema = types.Schema
FunctionDeclaration = types.FunctionDeclaration


# ─────────────────────────────────────────────────────────
# Canonical vibe IDs — derived from the single source of truth in
# ``app/bot/gift_taxonomy.py``. Exposing these as an enum on
# ``suggest_gifts.vibes`` lets Gemini itself do the natural-language →
# canonical-ID mapping (it's good at this, multilingual, zero latency tax)
# instead of relying on the fragile hardcoded keyword map that silently
# dropped tokens like 'fashion' / 'cooking' / 'reading' before we fixed it.
# Any new vibe added to gift_taxonomy automatically surfaces here.
# ─────────────────────────────────────────────────────────

_VIBE_IDS = all_vibe_ids()

# Short natural-language hint per vibe, for the schema description. Only
# the non-obvious IDs need a hint — most (gym_fitness, book_lover, …)
# are self-descriptive. Hints are deliberately English-only; Gemini
# handles Hindi/Hinglish → English mapping internally before matching.
_VIBE_HINTS: dict[str, str] = {
    "gen_z_streetwear": "trendy fashion, streetwear, hype brands, Supreme-style",
    "ethnic_fusion": "modern ethnic wear, Indo-western, fusion",
    "premium_western": "formal western, office wear, dress shirts",
    "luxury_fashion": "designer, high-end fashion",
    "ethnic_wear_occasion": "wedding / festival ethnic wear, sherwanis, lehengas",
    "ayurvedic_natural": "natural skincare, Ayurveda, herbal, organic",
    "mens_grooming": "beard care, shaving, men's skincare",
    "perfume_fragrance": "perfume, cologne, fragrance",
    "skincare_modern": "modern skincare, Korean beauty, glow",
    "makeup_premium": "premium makeup, lipstick, cosmetics",
    "food_delivery": "food delivery apps, Swiggy, Zomato",
    "gourmet_chocolate": "premium chocolate, Lindt, artisan",
    "bakery_cake": "cake, pastries, dessert, bakery",
    "mithai_traditional": "Indian sweets, mithai, Diwali sweets",
    "cooking_culinary": "cooking, chef, kitchenware, culinary gifts",
    "kitchen_essentials": "kitchen appliances, cookware, home essentials",
    "home_decor_modern": "home decor, interior, modern design, housewarming",
    "plants_garden": "plants, gardening, green thumb",
    "kids_learning": "kids, babies, newborn, children's gifts",
    "pet_parent": "pet owner, dog, cat, pet parent",
    "book_lover": "books, reading, literature, novels, Kindle",
    "audiobooks_podcasts": "audiobooks, podcasts, Audible",
    "audio_lover": "earbuds, headphones, speakers, audio gear",
    "mass_electronics": "gadgets, electronics, smartphones",
    "premium_apple": "Apple products, iPhone, MacBook, premium tech",
    "movies_ott": "movies, OTT, streaming, Netflix",
    "music_streaming": "music, Spotify, music streaming",
    "live_events": "concerts, shows, live events",
    "private_theatre": "private theatre experience, movie date",
    "travel_voucher": "travel, vacation, trip vouchers",
    "adventure_experiences": "adventure, trekking, outdoor experiences",
    "flowers_classic": "flowers, roses, bouquets",
    "fresh_flowers_local": "fresh flowers from a local florist",
    "fashion_jewellery": "fashion jewellery, costume jewellery",
    "silver_everyday": "silver jewellery, everyday wear",
    "fine_gold_diamond": "gold jewellery, diamonds, fine jewellery",
    "personalized_keepsake": "personalised gifts, keepsakes, custom-made",
    "memory_engraving": "engraved gifts, photo frames, memory items",
    "sneakerhead": "sneakers, limited drops, shoe collector",
    "bags_wallets": "bags, wallets, leather goods",
    "watches_accessories": "watches, fashion accessories",
    "gym_fitness": "gym, workout, fitness, athletic wear",
    "yoga_meditation": "yoga, meditation, mindfulness",
    "sports_outdoor": "outdoor sports, cycling, team sports",
    "home_workout": "home workout, home gym equipment",
    "wellness_recovery": "spa, massage, salon, recovery",
    "nutrition_supplements": "protein, supplements, fitness nutrition",
    "coffee_lover": "coffee, Starbucks, espresso, cafes",
    "tea_chai": "chai, tea, tea lover",
    "healthy_snacks": "healthy snacking, protein bars, clean food",
    "gourmet_hampers": "gourmet gift hampers, curated hampers",
    "bedding_linens": "bedsheets, bedding, home linens",
}


def _vibes_description() -> str:
    """Build the description block for ``suggest_gifts.vibes``.

    Lists every canonical vibe ID (from gift_taxonomy — single source of
    truth) alongside a short English hint so Gemini can map user
    language → canonical IDs without relying on our internal keyword map.
    """
    lines = [
        "Canonical vibe IDs the recipient matches. Pick 1-3 that best describe "
        "the recipient's interests; prefer more specific over more general. "
        "Multiple IDs sharpen the recommendation. Gemini: use your knowledge of "
        "the user's words (any language) to map to these IDs — the names are "
        "self-descriptive and the hints below cover the non-obvious ones.",
        "",
    ]
    for vid in _VIBE_IDS:
        hint = _VIBE_HINTS.get(vid)
        if hint:
            lines.append(f"  {vid}: {hint}")
        else:
            lines.append(f"  {vid}")
    return "\n".join(lines)


_VIBES_DESCRIPTION = _vibes_description()


# ─────────────────────────────────────────────────────────
# Reusable field schemas
# ─────────────────────────────────────────────────────────

_OCCASION_TYPE_ENUM = [
    "BIRTHDAY", "ANNIVERSARY", "WEDDING",
    "DIWALI", "RAKSHA_BANDHAN", "VALENTINES", "HOLI", "EID",
    "CHRISTMAS", "NEW_YEAR",
    "MOTHERS_DAY", "FATHERS_DAY",
    "CUSTOM",
]

_RELATIONSHIP_ENUM = [
    "friend", "spouse", "parent", "sibling", "colleague", "other",
]

_SCENARIO_ENUM = ["self", "for_friend", "for_contact"]

# Vendor categories match the exact lowercase strings stored in the Vendor
# table (see app/models/vendor.py). Gemini MUST pick one of these — any
# other value will silently filter to zero rows in search_vendors.
_VENDOR_CATEGORY_ENUM = [
    "private_theatre", "cake", "jewellery", "gift_card", "experience",
    "decor", "personalised", "skincare", "chocolates", "bags",
]


def _str(description: str, *, enum: list[str] | None = None) -> Schema:
    kwargs: dict = {"type": Type.STRING, "description": description}
    if enum:
        kwargs["enum"] = enum
    return Schema(**kwargs)


def _int(description: str, *, minimum: int | None = None, maximum: int | None = None) -> Schema:
    kwargs: dict = {"type": Type.INTEGER, "description": description}
    if minimum is not None:
        kwargs["minimum"] = minimum
    if maximum is not None:
        kwargs["maximum"] = maximum
    return Schema(**kwargs)


def _bool(description: str) -> Schema:
    return Schema(type=Type.BOOLEAN, description=description)


def _arr(description: str, items: Schema) -> Schema:
    return Schema(type=Type.ARRAY, description=description, items=items)


def _obj(description: str, properties: dict[str, Schema], required: list[str] | None = None) -> Schema:
    kwargs: dict = {
        "type": Type.OBJECT,
        "description": description,
        "properties": properties,
    }
    if required:
        kwargs["required"] = required
    return Schema(**kwargs)


def _params(
    properties: dict[str, Schema],
    required: list[str] | None = None,
) -> Schema:
    return Schema(
        type=Type.OBJECT,
        properties=properties,
        required=required or [],
    )


def _decl(name: str, description: str, parameters: Schema | None = None) -> FunctionDeclaration:
    if parameters is None:
        parameters = Schema(type=Type.OBJECT, properties={})
    return FunctionDeclaration(name=name, description=description, parameters=parameters)


def _contract(
    *,
    when: str,
    use_when: list[str],
    do_not: list[str],
    important: list[str],
    output: str,
) -> str:
    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    return (
        f"WHEN TO USE:\n{when}\n\n"
        f"USE WHEN:\n{_bullets(use_when)}\n\n"
        f"DO NOT USE WHEN:\n{_bullets(do_not)}\n\n"
        f"IMPORTANT:\n{_bullets(important)}\n\n"
        f"OUTPUT:\n{output}"
    )


# ─────────────────────────────────────────────────────────
# Declarations
# ─────────────────────────────────────────────────────────

TOOL_DECLARATIONS: dict[str, FunctionDeclaration] = {
    "get_user_profile": _decl(
        "get_user_profile",
        _contract(
            when="Check the current user's profile, registration, or guest state.",
            use_when=[
                "Conversation starts and user state is unclear.",
                "User asks about their Wishkr account or profile.",
                "A write flow needs to know whether the user is registered.",
            ],
            do_not=[
                "User asks for orders; use list_orders.",
                "User asks for occasions; use get_my_occasions.",
                "User asks for reminder details; use reminder tools.",
            ],
            important=[
                "Use this before registration-sensitive flows when state is unclear.",
                "If user is a guest, invite them to complete registration.",
            ],
            output="User profile, registration state, and guest status.",
        ),
    ),

    "register_user": _decl(
        "register_user",
        _contract(
            when="Create or complete the current user's Wishkr profile.",
            use_when=[
                "User gives their name after onboarding.",
                "User wants to create a Wishkr account.",
                "A flow needs registration and the user has provided a name.",
            ],
            do_not=[
                "Name is missing; ask for it first.",
                "User asks to update occasions, orders, or reminders.",
                "City is not mentioned; omit city instead of guessing.",
            ],
            important=[
                "Pass city only when the user states it.",
                "Pass timezone only when the user states an IANA timezone or it is supplied by the client.",
                "Already-registered responses are successful outcomes.",
            ],
            output="Registered user profile and optional city match.",
        ),
        _params(
            {
                "name": _str("User's full or first name as they introduced themselves."),
                "city": _str("User's city in India. Optional; omit if not mentioned."),
                "timezone": _str("Optional IANA timezone, for example Asia/Kolkata or America/New_York. Never guess."),
            },
            required=["name"],
        ),
    ),

    "add_occasion": _decl(
        "add_occasion",
        _contract(
            when="Save an occasion the user wants to track for someone else.",
            use_when=[
                "User says \"Rahul's birthday is 12 May\".",
                "User wants reminders for a friend's anniversary.",
                "User adds a contact occasion with date and type.",
            ],
            do_not=[
                "User wants friends reminded about their own occasion; use setup_celebrant_occasion.",
                "User is correcting a recent duplicate occasion; use edit_occasion.",
                "Date or occasion type is missing; ask for the missing input.",
                "User gives only a relation such as cousin, friend, sister, or brother without a real name; ask for the person's name first.",
            ],
            important=[
                "Use CUSTOM with custom_label for unsupported occasion names.",
                "A tight-window warning means the user may want an immediate reminder.",
                "contact_phone is optional. When you have to ask for the person's name (ambiguous relation), ask for their number in the same message and pass it here if given. Storing it does NOT message the recipient — never promise to wish/message them.",
            ],
            output="Saved occasion id, contact id, reminder ids, and duplicate hints.",
        ),
        _params(
            {
                "contact_name": _str("Specific name of the person this occasion is for. Do not pass only a generic relation like cousin/friend/sister/brother; ask for the name first."),
                "occasion_type": _str(
                    "One of the supported occasion types. Use CUSTOM for anything not listed and also pass custom_label.",
                    enum=_OCCASION_TYPE_ENUM,
                ),
                "date_day": _int("Day of the month, 1-31.", minimum=1, maximum=31),
                "date_month": _int("Month of the year, 1-12.", minimum=1, maximum=12),
                "date_year": _int("Optional year, especially for one-time occasions such as weddings.", minimum=1900, maximum=2200),
                "recurring": _bool("Whether this repeats yearly. Defaults true for birthdays/anniversaries/festivals and false for weddings/custom events."),
                "custom_label": _str("Required iff occasion_type=CUSTOM. Short descriptor, e.g. 'housewarming'."),
                "relationship": _str("How the user knows this person.", enum=_RELATIONSHIP_ENUM),
                "contact_phone": _str("Indian phone number, 10-12 digits, no spaces. Optional — stored on the contact for later, does not trigger any message."),
                "recipient_city": _str("Recipient's city, if already known. Optional; used for delivery and local gift matching."),
            },
            required=["contact_name", "occasion_type", "date_day", "date_month"],
        ),
    ),

    "list_occasions": _decl(
        "list_occasions",
        _contract(
            when="Deprecated compatibility fallback for occasion listing.",
            use_when=[
                "Only when no better occasion-list tool is available.",
            ],
            do_not=[
                "get_my_occasions is available; use it instead.",
                "User asks for upcoming time windows; use get_upcoming_digest.",
            ],
            important=[
                "Prefer get_my_occasions for all normal flows.",
            ],
            output="Occasion list compatible with older flows.",
        ),
    ),

    "get_my_occasions": _decl(
        "get_my_occasions",
        _contract(
            when="List all occasions saved by the current user.",
            use_when=[
                "User asks \"what occasions do I have?\"",
                "User says \"show my birthdays\" or \"show my list\".",
                "You need an occasion_id for edit, delete, snooze, or details.",
            ],
            do_not=[
                "User asks what is coming up this week; use get_upcoming_digest.",
                "User asks for one occasion's deep details; use get_occasion_details.",
            ],
            important=[
                "Use ids returned here for owner-only occasion tools.",
                "An empty list can be a valid result.",
            ],
            output="Saved occasions with ids, names, types, and dates.",
        ),
    ),

    "get_occasion_details": _decl(
        "get_occasion_details",
        _contract(
            when="Fetch details for one known saved occasion.",
            use_when=[
                "User picks a specific occasion from their list.",
                "You need contact_id or contact city for local vendors.",
                "You need wishlist links or celebrant details for an occasion.",
            ],
            do_not=[
                "You do not have a real occasion_id; call get_my_occasions first.",
                "User asks for all occasions; use get_my_occasions.",
            ],
            important=[
                "Never invent occasion ids.",
                "Use this before set_contact_city or local vendor lookup.",
            ],
            output="Occasion/contact details, reminder schedule, gift-plan summary, recipient wishlist count, giver-shortlist count, and celebrant context.",
        ),
        _params(
            {"occasion_id": _str("The occasion id from get_my_occasions.")},
            required=["occasion_id"],
        ),
    ),

    "search_brands": _decl(
        "search_brands",
        _contract(
            when="Catalogue lookup for a specific brand, category, or voucher.",
            use_when=[
                "User asks \"do you have Amazon?\"",
                "User asks for Starbucks, Swiggy, Zomato, Myntra, or similar.",
                "User asks what vouchers exist under a budget.",
            ],
            do_not=[
                "User asks for open-ended gift ideas; use suggest_gifts.",
                "User needs exact denominations for one chosen brand; use get_brand_details.",
            ],
            important=[
                "Use query for named brands or categories.",
                "Follow with get_brand_details before ordering if amount details are needed.",
            ],
            output="Matching voucher brands and high-level catalogue details.",
        ),
        _params(
            {
                "query": _str("Brand name, category, or keyword. Optional."),
                "budget_max": _int("Upper budget bound in rupees. Omit if unspecified.", minimum=1),
            },
        ),
    ),

    "suggest_gifts": _decl(
        "suggest_gifts",
        _contract(
            when="Open-ended gift recommendation requests.",
            use_when=[
                "User asks \"what should I gift?\"",
                "User asks for ideas by vibe, budget, occasion, or recipient type.",
                "User does not know the exact brand.",
            ],
            do_not=[
                "User asks for a specific brand or voucher; use search_brands.",
                "User asks for exact denominations; use get_brand_details.",
                "User asks for marketplace products; use suggest_marketplace_gift_card.",
            ],
            important=[
                "Pass occasion_id when known.",
                "Preserve recipient_vibe in the user's own words unless using canonical vibes.",
                "Use scenario='self' only for self-gift ideas.",
                "When recommendation_review is returned, use its top picks and at most one clarifying question.",
                "Only catalogue_options are confirmed Wishkr browse choices; never present generated general ideas as stocked products.",
                "Use additional general ideas only when wishkr_catalogue_match_found is false.",
            ],
            output="Ranked gift suggestions, optional context-aware review, exact Wishkr catalogue browse options, and claimed-gift signals.",
        ),
        _params(
            {
                "scenario": _str(
                    "'self' when the user is asking for themselves; 'for_friend' when gifting someone with an occasion in our system; 'for_contact' for a generic gift to a contact not tied to an occasion.",
                    enum=_SCENARIO_ENUM,
                ),
                "occasion_id": _str("Always pass when known — enables city-aware matching and claimed-gift dedupe."),
                "recipient_vibe": _str(
                    "The user's own words describing the recipient (e.g. 'gym guy', "
                    "'techie', 'loves coffee'). If you can pick canonical vibe IDs "
                    "directly from the user's description, prefer `vibes` over this."
                ),
                "vibes": _arr(
                    _VIBES_DESCRIPTION,
                    _str("Canonical vibe ID (must match the enum).", enum=_VIBE_IDS),
                ),
                "budget_min": _int("Lower budget bound in rupees. Omit unless explicit.", minimum=1),
                "budget_max": _int("Upper budget bound in rupees. Omit unless explicit.", minimum=1),
                "budget_text": _str("Free-form budget like '1k-3k', 'around 2000'. Prefer this over min/max when the user gives a fuzzy range."),
                "n": _int("How many suggestions to return. 5-8 default, 15 for 'show more', max 20.", minimum=1, maximum=20),
            },
            # scenario is optional — the server defaults to 'for_friend' when
            # absent. Enum still constrains the value IF the model sends it.
            required=[],
        ),
    ),

    "save_gift_plan": _decl(
        "save_gift_plan",
        _contract(
            when="Save or update private planning details for one known occasion.",
            use_when=[
                "User states a budget, interests, desired/excluded categories, brands, format, or constraints for a gift.",
                "User refines an existing occasion-specific gift plan.",
                "You need durable planning state before or after suggest_gifts.",
            ],
            do_not=[
                "User only wants to save an occasion/reminder; use add_occasion.",
                "You do not have a valid occasion_id; call get_my_occasions first.",
                "The information came from the recipient directly; use recipient-preference flows instead.",
            ],
            important=[
                "These fields are private to this giver and this occasion.",
                "Pass only fields the user actually stated; omit unknown fields.",
                "Empty arrays intentionally clear a saved list.",
            ],
            output="The saved occasion-specific gift-plan summary.",
        ),
        _params(
            {
                "occasion_id": _str("The occasion this private plan belongs to."),
                "budget_min": _int("Optional minimum gift budget in rupees.", minimum=1),
                "budget_max": _int("Optional maximum gift budget in rupees.", minimum=1),
                "currency": _str("ISO currency code. Defaults to INR."),
                "interests": _arr("Giver-observed recipient interests.", _str("One interest.")),
                "vibe_ids": _arr(_VIBES_DESCRIPTION, _str("Canonical vibe ID.", enum=_VIBE_IDS)),
                "desired_categories": _arr("Gift categories the giver wants to explore.", _str("One category.")),
                "excluded_categories": _arr("Gift categories to avoid.", _str("One excluded category.")),
                "preferred_brands": _arr("Brands the giver prefers.", _str("One preferred brand.")),
                "disliked_brands": _arr("Brands the giver wants to avoid.", _str("One disliked brand.")),
                "gift_format": _str(
                    "Preferred format.",
                    enum=["physical", "digital", "experience", "any"],
                ),
                "constraints": _obj(
                    "Optional category-specific constraints.",
                    {
                        "size": _str("Clothing or product size."),
                        "colour": _str("Preferred colour."),
                        "platform": _str("Gaming or device platform."),
                        "art_medium": _str("Painting, sketching, digital, etc."),
                        "notes": _str("Other concise constraints."),
                    },
                ),
                "recipient_city": _str("Delivery city override for this gift plan."),
            },
            required=["occasion_id"],
        ),
    ),

    "select_gift_candidate": _decl(
        "select_gift_candidate",
        _contract(
            when="Record which ranked gift option the user selected.",
            use_when=[
                "User chooses one item from suggest_gifts results.",
                "You have provider_id and item_id/candidate_id from a suggestion.",
                "User says 'go with option 2' after you know which candidate that is.",
            ],
            do_not=[
                "User has not selected a concrete candidate.",
                "You only have a brand name; use get_brand_details or search_brands.",
            ],
            important=[
                "Pass provider_id from the suggestion's provider_id/source.",
                "Pass candidate_id from the suggestion's item_id.",
            ],
            output="Selected candidate details saved into active gift context.",
        ),
        _params(
            {
                "provider_id": _str("Provider/source id from the selected suggestion, e.g. hubble or vendor_db."),
                "candidate_id": _str("Candidate item_id from the selected suggestion."),
            },
            required=["provider_id", "candidate_id"],
        ),
    ),

    "create_checkout": _decl(
        "create_checkout",
        _contract(
            when="Create checkout for a selected marketplace gift candidate.",
            use_when=[
                "User selected a suggestion and confirmed the amount.",
                "Use after select_gift_candidate when the user says pay/checkout/proceed.",
                "For Hubble candidates this creates a Razorpay payment link; for manual vendors this creates manual review.",
            ],
            do_not=[
                "No selected candidate is known; call select_gift_candidate first.",
                "Amount is missing or unclear.",
                "User is only browsing options.",
            ],
            important=[
                "provider_id/candidate_id may be omitted only if select_gift_candidate was called earlier in this active context.",
                "Use duplicate_acknowledged only after explicit confirmation.",
                "Manual vendors do not get fake instant payment links.",
            ],
            output="Payment link for API providers or manual-review order for manual vendors.",
        ),
        _params(
            {
                "amount": _int("Confirmed checkout amount in rupees.", minimum=1),
                "provider_id": _str("Provider/source id. Optional if already selected in active context."),
                "candidate_id": _str("Candidate item_id. Optional if already selected in active context."),
                "occasion_id": _str("Attach the checkout to this owned occasion if known."),
                "celebrant_occasion_id": _str("Friend-flow context id when an invited friend is buying for a celebrant."),
                "buyer_name": _str("Friend buyer name, required when celebrant_occasion_id is used."),
                "wishlist_item_id": _str("Optional wishlist item this checkout fulfills."),
                "special_instructions": _str("Manual vendor instructions, delivery notes, or customization details."),
                "duplicate_acknowledged": _bool("Set true only after duplicate warning and explicit user confirmation."),
            },
            required=["amount"],
        ),
    ),

    "get_self_preferences": _decl(
        "get_self_preferences",
        _contract(
            when="Read the current user's own gift preferences.",
            use_when=[
                "User asks \"what should I get for myself?\"",
                "User wants self-gift recommendations.",
                "You need saved preferences before suggest_gifts with scenario='self'.",
            ],
            do_not=[
                "User is gifting someone else; use recipient context instead.",
                "User asks for saved friend occasions; use get_my_occasions.",
            ],
            important=[
                "Call before self-gift recommendations.",
                "If no preferences exist, ask once for interests before suggesting.",
            ],
            output="Self preferences, own wishlists, and own celebrant occasions.",
        ),
    ),

    "suggest_marketplace_gift_card": _decl(
        "suggest_marketplace_gift_card",
        _contract(
            when="Suggest marketplace vouchers when user asks about marketplace products.",
            use_when=[
                "User asks for something from Amazon or Flipkart.",
                "User wants a Myntra, Nykaa, Ajio, or Croma gift option.",
                "User asks for a marketplace product you cannot browse.",
            ],
            do_not=[
                "User asks for generic gift ideas; use suggest_gifts.",
                "User asks for a non-marketplace brand; use search_brands.",
            ],
            important=[
                "Do not invent marketplace products.",
                "Use the marketplace name the user mentioned.",
            ],
            output="Relevant marketplace voucher brand options.",
        ),
        _params(
            {
                "marketplace": _str("The marketplace the user mentioned, lowercased (amazon, flipkart, myntra, ...)."),
                "n": _int("How many voucher brands to return. 1-10, default 3.", minimum=1, maximum=10),
            },
            required=["marketplace"],
        ),
    ),

    "get_brand_details": _decl(
        "get_brand_details",
        _contract(
            when="Get exact denomination and amount limits for one chosen brand.",
            use_when=[
                "User selected a brand and you need valid amounts.",
                "User asks \"what amounts are available?\"",
                "Before creating an order when denomination details are unknown.",
            ],
            do_not=[
                "Brand is unclear; use search_brands first.",
                "User asks for open-ended ideas; use suggest_gifts.",
            ],
            important=[
                "Use exact brand_name from search results or prior tool output.",
                "Use this before order tools when amount validity is uncertain.",
            ],
            output="Denomination type, allowed amounts, min/max, and product id.",
        ),
        _params(
            {"brand_name": _str("Exact brand name as returned by search_brands.")},
            required=["brand_name"],
        ),
    ),

    "create_gift_order": _decl(
        "create_gift_order",
        _contract(
            when="Create a payment link for one confirmed gift-card purchase by the current user.",
            use_when=[
                "User has selected a brand and amount.",
                "User is ready to pay for one gift card.",
                "Owner is buying for themselves or one of their saved occasions.",
            ],
            do_not=[
                "Friend is buying for celebrant; use create_friend_gift_order.",
                "Multiple items should be paid together; use cart flow.",
                "Brand or amount is still unclear.",
            ],
            important=[
                "Do not call until brand and amount are confirmed.",
                "Pass occasion_id when tying purchase to a saved occasion.",
                "Use duplicate_acknowledged only after explicit confirmation.",
            ],
            output="Order id, purchase id, payment link, and payment slip.",
        ),
        _params(
            {
                "brand_name": _str("Exact brand name from search_brands / get_brand_details."),
                "amount": _int("Amount in rupees. Must be a valid denomination for the brand — call get_brand_details first to confirm. FIXED-denomination brands only accept specific values; FLEXIBLE brands accept any amount between their min and max.", minimum=1),
                "occasion_id": _str("Attach the order to this occasion if known — enables reminders and dedupe."),
                "wishlist_item_id": _str("Optional `wsh_*` id of the wishlist item this order fulfils. When set, the item is marked gifted on payment success so suggest_gifts stops surfacing it next year. Pass it when the user explicitly says 'order this from my wishlist' or similar."),
                "duplicate_acknowledged": _bool("Set true only after the tool warned that this brand is already claimed for the occasion and the buyer explicitly said to proceed anyway."),
            },
            required=["brand_name", "amount"],
        ),
    ),

    "list_orders": _decl(
        "list_orders",
        _contract(
            when="Show the user's recent Wishkr orders and payment status.",
            use_when=[
                "User asks \"show my orders\".",
                "User asks whether a recent payment/order exists.",
                "User asks for purchase history.",
            ],
            do_not=[
                "User asks for gift spend total; use get_monthly_spend.",
                "User asks for cart contents; use view_cart.",
            ],
            important=[
                "Treat this as recent history, not a full archive.",
                "Use order status exactly as returned.",
            ],
            output="Recent orders with statuses and purchase details.",
        ),
    ),

    "check_occasion_gifts": _decl(
        "check_occasion_gifts",
        _contract(
            when="Check gifts already claimed for one occasion to avoid duplicates.",
            use_when=[
                "Before suggesting gifts for a known occasion.",
                "Before ordering a gift tied to an occasion.",
                "Friend wants to know what is already taken.",
            ],
            do_not=[
                "You do not have an occasion_id.",
                "User asks for all occasion details; use get_occasion_details.",
            ],
            important=[
                "Use only authorized occasion ids from owner or friend context.",
                "Respect privacy_note and do not reveal withheld details.",
            ],
            output="Taken categories, taken brands, and privacy guidance.",
        ),
        _params(
            {"occasion_id": _str("The occasion id to inspect.")},
            required=["occasion_id"],
        ),
    ),

    "log_external_purchase": _decl(
        "log_external_purchase",
        _contract(
            when="Record a gift the user already bought outside Wishkr.",
            use_when=[
                "User says they already bought a cake, book, item, or voucher elsewhere.",
                "Friend wants to mark a non-Wishkr gift as taken.",
                "User wants dedupe tracking without payment.",
            ],
            do_not=[
                "User wants to buy through Wishkr; use order tools.",
                "User is only considering a gift, not bought it.",
            ],
            important=[
                "This does not create payment or fulfillment.",
                "Pass wishlist_item_id when the external gift fulfills a saved wishlist item.",
            ],
            output="External gift claim record and dedupe result.",
        ),
        _params(
            {
                "occasion_id": _str("The occasion this external gift is for."),
                "item_description": _str("Short human description of what was bought (e.g. 'chocolate cake from local bakery')."),
                "category": _str("Loose free-text category for the external gift — e.g. fashion, books, gadgets, beauty, food, experiences, gift_card, other. Lowercased by the server."),
                "brand_name": _str("Brand or vendor name if known. Optional."),
                "wishlist_item_id": _str("Optional `wsh_*` id of the wishlist item this purchase fulfills — pass it when the buyer is logging an external buy of a saved wishlist URL so the item is marked gifted and suggest_gifts stops surfacing it."),
            },
            required=["occasion_id", "item_description"],
        ),
    ),

    "log_my_gift": _decl(
        "log_my_gift",
        _contract(
            when="Remember a gift the user already gave someone, for future no-repeat suggestions.",
            use_when=[
                "User says they gave/bought a specific gift for a person (e.g. 'I gave Riya a watch last year').",
                "User wants Wishkr to remember what they gifted so it won't suggest the same thing again.",
            ],
            do_not=[
                "User is asking for ideas, not recording a past gift; use suggest_gifts.",
                "Gift is for a shared/celebrant occasion dedupe; use log_external_purchase.",
            ],
            important=[
                "This is memory only — no payment, fulfillment, or message to the recipient.",
                "Pass recipient_name so it links to the right contact; occasion_id is optional.",
            ],
            output="Saved gift-log id and the resolved recipient name.",
        ),
        _params(
            {
                "description": _str("Short description of the gift the user gave (e.g. 'wireless headphones', 'a watch')."),
                "recipient_name": _str("Name of the person who received the gift. Used to link the log to a saved contact."),
                "contact_id": _str("Optional `con_*` id of the recipient contact, when already known from context."),
                "occasion_id": _str("Optional `occ_*` id of the occasion this gift was for."),
                "gift_year": _str("Optional 4-digit year the gift was given, e.g. '2025'."),
            },
            required=["description"],
        ),
    ),

    "add_to_wishlist": _decl(
        "add_to_wishlist",
        _contract(
            when="Save a product URL to a self or gift wishlist.",
            use_when=[
                "User shares a product link and says save it.",
                "User wants to remember a link for later.",
                "Friend saves a gift idea URL for a celebrant occasion.",
                "User wants to add a URL to their birthday or occasion-specific wishlist.",
            ],
            do_not=[
                "User describes a product but gives no URL; ask for the link.",
                "User wants to buy immediately; use order tools.",
                "User wants marketplace browsing; use suggest_marketplace_gift_card.",
            ],
            important=[
                "Pass the URL exactly as provided.",
                "Use purpose='gift' only when a valid recipient_occasion_id/FriendContext is available; otherwise omit purpose or use self.",
                "Saved links are not automatically attached to celebrant setup.",
                "Pass own_occasion_id when user says 'add to my birthday wishlist' or refers to a specific personal occasion.",
                "Pass visibility='friends' only when user explicitly says they want friends to see this item.",
            ],
            output="Wishlist item id, saved URL, and duplicate/limit status.",
        ),
        _params(
            {
                "url": _str("The product URL exactly as the user shared it. Required. Must appear in the user's message — do not fabricate."),
                "purpose": _str("'self' if the user wants this product for themselves, 'gift' if they're saving it as a gift idea for someone else's occasion. Defaults to 'self' when omitted.", enum=["self", "gift"]),
                "recipient_occasion_id": _str("Recipient context id for the gift. REQUIRED when purpose='gift'. Use the active FriendContext id in friend flow, or an owned occasion_id in normal gift flow. Omit for purpose='self'."),
                "own_occasion_id": _str("Optional. The user's own occasion id (occ_*) to link this self-wishlist item to a specific personal occasion (e.g. 'add to my birthday wishlist'). Omit if not occasion-specific. Only valid for purpose='self'."),
                "visibility": _str("Who can see this item. 'private' (default) = only owner. 'friends' = invited friends on the linked occasion can view. Only for purpose='self'.", enum=["private", "friends", "public"]),
                "remind_on": _str("ISO date YYYY-MM-DD when the user wants a reminder about this item ('remind me on April 29'). Must be a future date. Resolve relative dates ('next Tuesday', 'in two weeks') against the Today value in turn context before passing. Omit if the user didn't ask for a reminder."),
            },
            required=["url"],
        ),
    ),

    "remove_wishlist_item": _decl(
        "remove_wishlist_item",
        _contract(
            when="Remove a single item from the user's personal wishlist.",
            use_when=[
                "User says remove/delete a specific item from their wishlist.",
                "User identifies an item by URL or position and asks to remove it.",
            ],
            do_not=[
                "User wants to clear everything; use clear_wishlist.",
                "You don't know which URL to remove; call list_my_wishlist first.",
            ],
            important=[
                "Call list_my_wishlist first if you don't already have the item's URL.",
            ],
            output="Success status and confirmation message.",
        ),
        _params(
            {"url": _str("The exact URL of the wishlist item to remove.")},
            required=["url"],
        ),
    ),

    "clear_wishlist": _decl(
        "clear_wishlist",
        _contract(
            when="Delete all items from the user's personal wishlist at once.",
            use_when=[
                "User says clear/reset/empty their wishlist.",
                "User says remove everything from wishlist.",
            ],
            do_not=[
                "User wants to remove a single item; use remove_wishlist_item.",
            ],
            important=[
                "This is irreversible — only call after the user explicitly confirms.",
            ],
            output="Count of items cleared.",
        ),
        _params({}, required=[]),
    ),

    "list_my_wishlist": _decl(
        "list_my_wishlist",
        _contract(
            when="Fetch the user's personal saved wishlist items (purpose='self' only).",
            use_when=[
                "User asks to see their wishlist.",
                "User asks what they have saved.",
                "User wants to review their saved product links.",
                "User asks 'show my birthday wishlist' — pass own_occasion_id.",
            ],
            do_not=[
                "User wants to add a link; use add_to_wishlist.",
                "User is in friend flow; wishlist there belongs to the celebrant occasion.",
                "User asks to see another person's wishlist; use get_friend_wishlist.",
                "Never call this for purpose='gift' items — those are private gift plans.",
            ],
            important=[
                "Call this before responding about wishlist contents — never assume empty.",
                "Returns only purpose='self' items — private gift plans are never included.",
                "Pass own_occasion_id to filter to a specific occasion-linked wishlist.",
            ],
            output="List of saved items with title and URL, plus total count.",
        ),
        _params(
            {
                "own_occasion_id": _str("Optional. Filter to items linked to this specific personal occasion (occ_*). Omit to show all personal wishlist items regardless of occasion."),
            },
            required=[],
        ),
    ),

    "get_upcoming_digest": _decl(
        "get_upcoming_digest",
        _contract(
            when="Find upcoming occasions in a time window.",
            use_when=[
                "User asks \"what's coming up?\"",
                "User asks for birthdays this week or next 7 days.",
                "User asks about their own upcoming reminder timing broadly.",
            ],
            do_not=[
                "User asks for the full saved list; use get_my_occasions.",
                "User asks for one occasion's details; use get_occasion_details.",
            ],
            important=[
                "Set include_own=true when user asks about their own birthday/anniversary or broad upcoming context.",
                "Use days when the user gives a time window.",
            ],
            output="Upcoming occasion ids and summary for the requested window.",
        ),
        _params(
            {
                "days": _int("Window size in days. Default 7.", minimum=1, maximum=365),
                "include_own": _bool("Include the user's OWN celebrant occasions. Default false."),
            },
        ),
    ),

    "snooze_occasion": _decl(
        "snooze_occasion",
        _contract(
            when="Delay the next reminder for one saved occasion.",
            use_when=[
                "User says \"remind me later\" for an occasion.",
                "User wants to push a reminder by a few days.",
                "User asks to snooze, not delete.",
            ],
            do_not=[
                "User wants to skip the whole year; use skip_occasion_this_year.",
                "User wants permanent removal; use delete_occasion.",
            ],
            important=[
                "Use an occasion_id from the user's saved occasions.",
                "Default to a short delay when user does not specify days.",
            ],
            output="Updated snooze date for the occasion.",
        ),
        _params(
            {
                "occasion_id": _str("The occasion whose reminder to snooze."),
                "days": _int("How many days to delay. Default 2.", minimum=1, maximum=30),
            },
            required=["occasion_id"],
        ),
    ),

    "skip_occasion_this_year": _decl(
        "skip_occasion_this_year",
        _contract(
            when="Skip reminders for a saved occasion for the current year only.",
            use_when=[
                "User says \"skip this birthday this year\".",
                "User does not want reminders for this occurrence.",
                "User wants reminders to resume next year.",
            ],
            do_not=[
                "User wants a short delay; use snooze_occasion.",
                "User wants permanent deletion; use delete_occasion.",
            ],
            important=[
                "The occasion stays saved.",
                "Use an occasion_id from the user's saved occasions.",
            ],
            output="Skipped-year confirmation for the occasion.",
        ),
        _params(
            {"occasion_id": _str("The occasion to skip for this year.")},
            required=["occasion_id"],
        ),
    ),

    "get_monthly_spend": _decl(
        "get_monthly_spend",
        _contract(
            when="Answer how much the user spent on gifts this month.",
            use_when=[
                "User asks \"how much have I spent this month?\"",
                "User asks about monthly gift budget usage.",
                "User wants a current-month spend total.",
            ],
            do_not=[
                "User wants order history; use list_orders.",
                "User wants cart total; use view_cart.",
            ],
            important=[
                "This is a monthly total, not a transaction list.",
            ],
            output="Current-month gift spend total.",
        ),
    ),

    "create_batch_gift_orders": _decl(
        "create_batch_gift_orders",
        _contract(
            when="Create multiple independent gift-card orders at once.",
            use_when=[
                "User wants Amazon for Rahul and Starbucks for Priya in one message.",
                "User wants separate payment links per gift.",
                "User lists multiple confirmed brand-amount purchases.",
            ],
            do_not=[
                "User wants one combined bill; use add_to_cart then checkout_cart.",
                "Only one item is being bought; use create_gift_order.",
                "Any brand or amount is unconfirmed.",
            ],
            important=[
                "Each item needs brand_name and amount.",
                "Each item can succeed or fail independently.",
            ],
            output="Per-item order ids, payment links, and results.",
        ),
        _params(
            {
                "items": _arr(
                    "List of order objects. Each has brand_name (string), amount (integer rupees), and optional occasion_id (string).",
                    Schema(
                        type=Type.OBJECT,
                        properties={
                            "brand_name": _str("Exact brand name."),
                            "amount": _int("Rupees.", minimum=1),
                            "occasion_id": _str("Optional occasion id."),
                        },
                        required=["brand_name", "amount"],
                    ),
                ),
            },
            required=["items"],
        ),
    ),

    "setup_celebrant_occasion": _decl(
        "setup_celebrant_occasion",
        _contract(
            when="Set up the user's own occasion so friends can be reminded.",
            use_when=[
                "User says \"remind my friends about my birthday\".",
                "User gives their own occasion date and friend phones.",
                "User wants friends to see preferences or wishlist links.",
            ],
            do_not=[
                "User is tracking someone else's occasion; use add_occasion.",
                "User only wants to save a product link; use add_to_wishlist.",
                "The own occasion already exists and the user is editing preferences, wishlist, note, friends, date, or recurrence; use update_celebrant_occasion.",
            ],
            important=[
                "Preserve friend names in inputs like Name (919876543210).",
                "Pass wishlist_urls explicitly when user shared links for friends.",
                "Only occasion type and date are required to save the occasion.",
                "After creation, use update_celebrant_occasion for progressive edits.",
                "At least one valid friend phone is required before any targeted friend reminder can be sent.",
            ],
            output="Occasion id, celebrant occasion id, friend links, and reminder ids.",
        ),
        _params(
            {
                "occasion_type": _str("Type of the user's own occasion.", enum=_OCCASION_TYPE_ENUM),
                "date_day": _int("Day of the month, 1-31.", minimum=1, maximum=31),
                "date_month": _int("Month, 1-12.", minimum=1, maximum=12),
                "date_year": _int("Optional year, especially for a one-time wedding or custom event.", minimum=1900, maximum=2200),
                "recurring": _bool("Whether this repeats yearly. Defaults by occasion type."),
                "preferences": _arr(
                    "Optional gift categories the user would love (e.g. ['Gift Cards', 'Books', 'Gadgets']). Up to 8 items.",
                    _str("One preference category."),
                ),
                "friend_phones": _arr(
                    "Phone numbers of friends to remind. Accepts 'Name (919876543210)' or a bare phone.",
                    _str("A phone (optionally prefixed with 'Name (...)')."),
                ),
                "custom_label": _str("Required iff occasion_type=CUSTOM."),
                "wishlist_urls": _arr(
                    "Wishlist links to share with friends.",
                    Schema(
                        type=Type.OBJECT,
                        properties={
                            "url": _str("Wishlist URL."),
                            "label": _str("Short label the friends will see."),
                        },
                        required=["url"],
                    ),
                ),
                "note_to_friends": _str("Personal note included in the reminder. Optional."),
            },
            required=["occasion_type", "date_day", "date_month"],
        ),
    ),

    "update_celebrant_occasion": _decl(
        "update_celebrant_occasion",
        _contract(
            when="Progressively edit an existing occasion owned by the user.",
            use_when=[
                "User adds or clears their preferences, wishlist, or note.",
                "User adds or removes invited friends.",
                "User changes the date, year, recurrence, or custom label.",
            ],
            do_not=[
                "The own occasion does not exist yet; use setup_celebrant_occasion.",
                "User is tracking someone else's occasion; use edit_occasion.",
                "You do not have an owned occasion_id; call get_my_occasions first.",
            ],
            important=[
                "Omitted fields are preserved, including year and recurrence.",
                "Use add_friend_phones and remove_friend_phones independently.",
                "Empty preference/wishlist arrays intentionally clear those fields.",
            ],
            output="Updated own occasion, preferences, wishlist, friend counts, and reminder changes.",
        ),
        _params(
            {
                "occasion_id": _str("Owned occasion id from get_my_occasions."),
                "occasion_type": _str(
                    "Replacement occasion type.",
                    enum=_OCCASION_TYPE_ENUM,
                ),
                "date_day": _int("Replacement day, 1-31.", minimum=1, maximum=31),
                "date_month": _int("Replacement month, 1-12.", minimum=1, maximum=12),
                "date_year": _int("Replacement year.", minimum=1900, maximum=2200),
                "recurring": _bool("Whether the occasion repeats yearly."),
                "custom_label": _str("Replacement custom label; empty clears it."),
                "preferences": _arr("Complete replacement preference list.", _str("One preference.")),
                "wishlist_urls": _arr(
                    "Complete replacement wishlist-link list.",
                    _obj(
                        "One wishlist link.",
                        {
                            "url": _str("Wishlist URL."),
                            "label": _str("Short label."),
                        },
                        required=["url"],
                    ),
                ),
                "note_to_friends": _str("Replacement note; empty clears it."),
                "add_friend_phones": _arr(
                    "Friends to add; accepts Name (919876543210).",
                    _str("One friend phone."),
                ),
                "remove_friend_phones": _arr(
                    "Friends to remove from this occasion.",
                    _str("One friend phone."),
                ),
            },
            required=["occasion_id"],
        ),
    ),

    "get_celebrant_context": _decl(
        "get_celebrant_context",
        _contract(
            when="Friend-flow entrypoint for a reminder-link celebrant occasion.",
            use_when=[
                "Friend opens a gift reminder link.",
                "Friend asks what the celebrant wants.",
                "Friend needs celebrant preferences, wishlist, or claimed gifts.",
            ],
            do_not=[
                "Current user owns the occasion; use owner occasion tools.",
                "No celebrant_occasion_id is available.",
                "You are only browsing generic gift ideas.",
            ],
            important=[
                "Use the celebrant_occasion_id from the turn context.",
                "Do not ask for or reveal celebrant phone.",
            ],
            output="Celebrant occasion context, preferences, wishlists, and already-purchased status.",
        ),
        _params(
            {"celebrant_occasion_id": _str("The celebrant_occasion_id from turn context.")},
            required=["celebrant_occasion_id"],
        ),
    ),

    "create_friend_gift_order": _decl(
        "create_friend_gift_order",
        _contract(
            when="Create a payment link for a friend buying for a celebrant.",
            use_when=[
                "Friend has selected a brand and amount for the celebrant.",
                "Friend is ready to pay from a reminder-link flow.",
                "Friend is fulfilling a celebrant wishlist item.",
            ],
            do_not=[
                "Owner is buying for their saved occasion; use create_gift_order.",
                "No valid friend context exists.",
                "Multiple items should be one combined cart checkout.",
            ],
            important=[
                "Call get_celebrant_context first in friend flows.",
                "Do not call until brand, amount, and buyer_name are confirmed.",
                "Use duplicate_acknowledged only after explicit confirmation.",
            ],
            output="Friend order id, purchase id, payment link, and payment slip.",
        ),
        _params(
            {
                "celebrant_occasion_id": _str("From turn context."),
                "brand_name": _str("Exact brand name."),
                "amount": _int("Rupees.", minimum=1),
                "buyer_name": _str("Name the celebrant will see as the gifter."),
                "personal_message": _str("Optional short note to the celebrant."),
                "wishlist_item_id": _str("Optional `wsh_*` id of the wishlist item being fulfilled — the celebrant's saved URL the friend is gifting against. Marks the item gifted on payment success."),
                "duplicate_acknowledged": _bool("Set true only after the tool warned that this brand is already claimed for the occasion and the buyer explicitly said to proceed anyway."),
            },
            required=["celebrant_occasion_id", "brand_name", "amount", "buyer_name"],
        ),
    ),

    "edit_occasion": _decl(
        "edit_occasion",
        _contract(
            when="Update an existing saved occasion.",
            use_when=[
                "User corrects a date, month, occasion type, or custom label.",
                "add_occasion returned a duplicate_recent correction target.",
                "User wants to rename the contact for a contact-linked occasion.",
            ],
            do_not=[
                "User wants to create a new occasion; use add_occasion.",
                "User wants to remove an occasion; use delete_occasion.",
                "No actual changed field is known.",
            ],
            important=[
                "Pass only fields the user wants changed.",
                "Use existing_occasion_id from duplicate_recent when correcting.",
            ],
            output="Updated occasion details.",
        ),
        _params(
            {
                "occasion_id": _str("The occasion to edit."),
                "date_day": _int("New day, 1-31.", minimum=1, maximum=31),
                "date_month": _int("New month, 1-12.", minimum=1, maximum=12),
                "occasion_type": _str("New type.", enum=_OCCASION_TYPE_ENUM),
                "custom_label": _str("New label (CUSTOM only)."),
                "contact_name": _str("Updated contact name."),
            },
            required=["occasion_id"],
        ),
    ),

    "delete_occasion": _decl(
        "delete_occasion",
        _contract(
            when="Permanently delete a saved occasion after confirmation.",
            use_when=[
                "User says delete/remove this occasion.",
                "User confirms they want reminders cancelled.",
                "User wants the occasion gone permanently.",
            ],
            do_not=[
                "User wants to skip only this year; use skip_occasion_this_year.",
                "User wants a short delay; use snooze_occasion.",
                "User has not explicitly confirmed deletion.",
            ],
            important=[
                "Deletion also cancels pending reminders.",
                "Use an occasion_id from the user's saved occasions.",
            ],
            output="Deleted occasion id and cancelled reminder count.",
        ),
        _params(
            {"occasion_id": _str("The occasion to delete.")},
            required=["occasion_id"],
        ),
    ),

    "merge_occasions": _decl(
        "merge_occasions",
        _contract(
            when="Combine two duplicate saved occasions into one canonical occasion.",
            use_when=[
                "User says two listed occasions are the same person/event.",
                "User asks to combine, merge, or deduplicate two occasion IDs.",
                "The occasions have the same type and day/month.",
            ],
            do_not=[
                "User wants permanent removal only; use delete_occasion.",
                "Date or type differs and should be corrected; use edit_occasion first.",
                "You do not know which occasion to keep and which to merge.",
            ],
            important=[
                "Call get_my_occasions first if IDs are not already known.",
                "Use contact_name to rename the kept contact, e.g. sister → Rutuja.",
                "Moves reminders, purchases, claims, and occasion-scoped wishlist items.",
            ],
            output="Canonical occasion id plus moved dependent row counts.",
        ),
        _params(
            {
                "keep_occasion_id": _str("Canonical occasion to keep."),
                "merge_occasion_id": _str("Duplicate occasion to merge then delete."),
                "contact_name": _str("Optional final contact name for the kept occasion."),
            },
            required=["keep_occasion_id", "merge_occasion_id"],
        ),
    ),

    "add_to_cart": _decl(
        "add_to_cart",
        _contract(
            when="Add one confirmed gift-card item to the current cart.",
            use_when=[
                "User wants to buy multiple gifts together.",
                "User says add this brand and amount to cart.",
                "User is not ready to checkout yet.",
            ],
            do_not=[
                "User wants one immediate payment link; use create_gift_order.",
                "Friend is buying in celebrant flow; use create_friend_gift_order.",
                "Brand or amount is unclear.",
            ],
            important=[
                "Use cart flow for one combined checkout.",
                "Cart items expire after 24 hours.",
            ],
            output="Added cart item and current cart state.",
        ),
        _params(
            {
                "brand_name": _str("Exact brand name."),
                "amount": _int("Rupees.", minimum=1),
                "occasion_id": _str("Optional occasion to attach."),
            },
            required=["brand_name", "amount"],
        ),
    ),

    "view_cart": _decl(
        "view_cart",
        _contract(
            when="Show the user's current cart.",
            use_when=[
                "User asks \"what's in my cart?\"",
                "User asks for cart total.",
                "Before confirming checkout.",
            ],
            do_not=[
                "User wants to add an item; use add_to_cart.",
                "User wants to pay now; use checkout_cart after confirmation.",
            ],
            important=[
                "Use returned expiry status if present.",
                "This does not create an order or payment link.",
            ],
            output="Cart items, cart total, and expiry status.",
        ),
    ),

    "checkout_cart": _decl(
        "checkout_cart",
        _contract(
            when="Checkout staged cart items into payment links.",
            use_when=[
                "User confirms they want to pay for the cart.",
                "User says checkout or proceed to payment.",
                "Cart has multiple confirmed items.",
            ],
            do_not=[
                "Cart is only being reviewed; use view_cart.",
                "User wants to remove/abandon cart; use clear_cart.",
                "User wants one immediate purchase outside cart; use create_gift_order.",
            ],
            important=[
                "Each cart item can succeed or fail independently.",
                "Inspect per-item results before replying.",
            ],
            output="Order ids, payment links, and per-item checkout results.",
        ),
    ),

    "clear_cart": _decl(
        "clear_cart",
        _contract(
            when="Clear the current cart without creating payment links.",
            use_when=[
                "User says clear/cancel my cart.",
                "User changes their mind before checkout.",
                "User wants to start cart selection over.",
            ],
            do_not=[
                "User wants to cancel an existing order or payment link.",
                "User only asks to view cart; use view_cart.",
            ],
            important=[
                "This only affects staged cart items.",
                "It has no payment side effects.",
            ],
            output="Number of cart items cleared.",
        ),
    ),

    "search_vendors": _decl(
        "search_vendors",
        _contract(
            when="Find direct partners for physical gifts or local experiences.",
            use_when=[
                "User asks for cake, flowers, theatre, jewellery, decor, or experiences.",
                "User wants local options for a known recipient city.",
                "Gift idea needs partner vendors rather than vouchers.",
            ],
            do_not=[
                "User asks for gift cards; use search_brands.",
                "User asks open-ended gift ideas; use suggest_gifts.",
                "Category is not one of the supported vendor categories.",
            ],
            important=[
                "Pass occasion_id when local city-aware results are needed.",
                "Use get_occasion_details and set_contact_city when city is missing.",
            ],
            output="National and local vendor matches for the category.",
        ),
        _params(
            {
                "category": _str(
                    "Exact vendor category. Lowercase, underscore-separated.",
                    enum=_VENDOR_CATEGORY_ENUM,
                ),
                "occasion_id": _str("Occasion id — scopes results to the recipient's city."),
            },
            required=["category"],
        ),
    ),

    "get_reminder_schedule": _decl(
        "get_reminder_schedule",
        _contract(
            when="Show upcoming reminder schedule for one saved occasion.",
            use_when=[
                "User asks when reminders will go out.",
                "User asks who will be reminded next.",
                "User asks about pending reminders for an occasion.",
            ],
            do_not=[
                "User asks what was already sent; use get_reminder_history.",
                "No occasion_id is known; use get_my_occasions first.",
            ],
            important=[
                "Do not guess reminder timing.",
                "Use this for future/pending reminder questions.",
            ],
            output="Pending reminder schedule and reminder targets.",
        ),
        _params(
            {"occasion_id": _str("Occasion id the user owns. Get it from get_my_occasions if needed.")},
            required=["occasion_id"],
        ),
    ),

    "get_reminder_history": _decl(
        "get_reminder_history",
        _contract(
            when="Show sent or past reminders for one saved occasion.",
            use_when=[
                "User asks whether reminders were sent.",
                "User asks reminder history for an occasion.",
                "User asks which reminders are pending versus sent.",
            ],
            do_not=[
                "User asks only about future schedule; use get_reminder_schedule.",
                "No occasion_id is known; use get_my_occasions first.",
            ],
            important=[
                "Use this for sent/history questions.",
                "Avoid calling schedule and history back-to-back unless the user needs both views.",
            ],
            output="Sent, pending, and other reminders for the occasion.",
        ),
        _params(
            {"occasion_id": _str("Occasion id the user owns.")},
            required=["occasion_id"],
        ),
    ),

    "set_reminder_schedule": _decl(
        "set_reminder_schedule",
        _contract(
            when="Replace pending reminders for one saved occasion with the exact day offsets the user requests.",
            use_when=[
                "User says remind me 1 day before and on the day.",
                "User asks to change an occasion's reminder schedule.",
                "User specifies days or weeks before an occasion.",
            ],
            do_not=[
                "User only asks what is currently scheduled; use get_reminder_schedule.",
                "User asks for an immediate message; use send_reminder_now.",
                "No occasion_id is known; use get_my_occasions first.",
            ],
            important=[
                "Convert weeks to days before calling, for example one week = 7.",
                "Use 0 for the occasion day.",
                "Only confirm offsets and dates returned by the tool.",
                "This replaces pending reminders to the chatting user; sent history and friend-targeted reminders stay unchanged.",
            ],
            output="Confirmed replacement schedule with exact offsets and timestamps.",
        ),
        _params(
            {
                "occasion_id": _str("Occasion id the user owns."),
                "days_before": _arr(
                    "Unique day offsets requested by the user. Example: [1, 0] means one day before and on the day.",
                    _int("Whole days before the occasion; 0 means on the day.", minimum=0, maximum=365),
                ),
            },
            required=["occasion_id", "days_before"],
        ),
    ),

    "send_reminder_now": _decl(
        "send_reminder_now",
        _contract(
            when="Send an immediate reminder after the user asks or confirms.",
            use_when=[
                "User says send the reminder now.",
                "User confirms sending to a specific linked friend phone.",
            ],
            do_not=[
                "User has not explicitly asked or confirmed sending.",
                "Targeted friend setup has not been completed; use setup_celebrant_occasion first.",
                "User only asks about reminder timing; use schedule/history tools.",
            ],
            important=[
                "Use target_phone only after setup_celebrant_occasion has succeeded for this occasion and linked that phone.",
                "Respect rate_limited responses and do not retry immediately.",
            ],
            output="Sent reminder id, recipient, and WhatsApp message id.",
        ),
        _params(
            {
                "occasion_id": _str("Occasion id the user owns."),
                "target_phone": _str("Specific friend phone to remind (celebrant flow only). Requires setup_celebrant_occasion/CelebrantOccasion for this occasion. Omit to reach the occasion owner's reminder stream."),
            },
            required=["occasion_id"],
        ),
    ),

    "set_contact_city": _decl(
        "set_contact_city",
        _contract(
            when="Save a recipient contact's city for local vendor matching.",
            use_when=[
                "Local vendor search needs the recipient city.",
                "get_occasion_details shows contact city is missing.",
                "User provides the city for a known occasion contact.",
            ],
            do_not=[
                "You only have a contact name; get contact_id first.",
                "User asks for national vendors only.",
                "User is updating their own profile city; use register_user when relevant.",
            ],
            important=[
                "Call get_occasion_details first to get contact_id.",
                "Pass the city exactly as the user provided it.",
            ],
            output="Updated contact id and resolved city.",
        ),
        _params(
            {
                "contact_id": _str("Contact id from an occasion."),
                "city": _str("City name in India."),
            },
            required=["contact_id", "city"],
        ),
    ),

    "get_friend_wishlist": _decl(
        "get_friend_wishlist",
        _contract(
            when="Fetch the celebrant's native Wishkr wishlist items visible to the calling friend.",
            use_when=[
                "Friend asks 'what is on Rahul's wishlist?'",
                "Friend wants to browse available items on a celebrant's native wishlist.",
                "After get_celebrant_context returned wishlist items exist.",
            ],
            do_not=[
                "Owner views their own wishlist; use list_my_wishlist.",
                "No celebrant_occasion_id is available.",
                "Celebrant has no native Wishkr items (only external URLs); use wishlists from get_celebrant_context.",
            ],
            important=[
                "Only returns purpose='self' items the celebrant has shared with friends (visibility='friends').",
                "Does NOT return purpose='gift' items — those are private gift plans of the caller.",
                "Reserved or purchased items are shown as unavailable — do not suggest buying them.",
                "Never reveal who reserved or purchased an item — surprise mode is always respected.",
                "Call get_celebrant_context first to confirm the wishlist exists.",
            ],
            output="Available wishlist items with share_status, excluding purchaser identity.",
        ),
        _params(
            {
                "celebrant_occasion_id": _str("The celebrant_occasion_id from turn context or get_celebrant_context."),
            },
            required=["celebrant_occasion_id"],
        ),
    ),

    "reserve_wishlist_item": _decl(
        "reserve_wishlist_item",
        _contract(
            when="Claim a specific wishlist item so other friends know it is taken.",
            use_when=[
                "Friend says 'I want to buy this item from the wishlist'.",
                "Friend selects a specific item from get_friend_wishlist results.",
                "Before purchasing to prevent another friend buying the same thing.",
            ],
            do_not=[
                "Item is already reserved or purchased; tell friend to pick another item.",
                "Friend has not viewed the wishlist yet; call get_friend_wishlist first.",
                "Owner cannot reserve their own items.",
                "Do not call unless the friend has explicitly confirmed which item they want.",
            ],
            important=[
                "Reservation expires after 48 hours if not confirmed as purchased.",
                "Another friend cannot reserve the same item while it is reserved.",
                "The celebrant/owner is never told who reserved or purchased an item.",
                "On success, tell the friend their reservation expires_at so they know the window.",
            ],
            output="Reservation status, wishlist_item_id, and reservation_expires_at.",
        ),
        _params(
            {
                "wishlist_item_id": _str("The wsh_* id of the wishlist item to reserve, from get_friend_wishlist."),
                "celebrant_occasion_id": _str("The celebrant_occasion_id from turn context — authorizes access."),
            },
            required=["wishlist_item_id", "celebrant_occasion_id"],
        ),
    ),

    "confirm_wishlist_purchase": _decl(
        "confirm_wishlist_purchase",
        _contract(
            when="Mark a reserved wishlist item as purchased so others can't buy it.",
            use_when=[
                "Friend says 'I bought it' or 'I purchased this item'.",
                "Friend has completed payment and wants to mark the item as done.",
                "After a successful order or external purchase of a reserved wishlist item.",
            ],
            do_not=[
                "Friend has not reserved the item first; call reserve_wishlist_item.",
                "Friend is only considering the item, not confirmed purchase.",
                "Never mark as purchased unless the buyer explicitly confirms.",
            ],
            important=[
                "Only the friend who reserved the item can mark it as purchased.",
                "Purchased items are permanently hidden from other friends as available.",
                "The celebrant never sees purchase state or purchaser identity.",
                "Links to create_friend_gift_order or log_external_purchase if available.",
            ],
            output="Purchase confirmation, share_status='purchased', and privacy assurance.",
        ),
        _params(
            {
                "wishlist_item_id": _str("The wsh_* id of the wishlist item, from reserve_wishlist_item."),
                "celebrant_occasion_id": _str("The celebrant_occasion_id from turn context — authorizes access."),
            },
            required=["wishlist_item_id", "celebrant_occasion_id"],
        ),
    ),

    "request_recipient_preferences": _decl(
        "request_recipient_preferences",
        _contract(
            when="Ask the gift-giver for permission then message the recipient to collect gift preferences.",
            use_when=[
                "Gift-giver asks what to buy for someone with no wishlist or preferences.",
                "Gift-giver explicitly agrees to let the bot ask the recipient.",
                "No wishlist or preference data exists for the recipient.",
            ],
            do_not=[
                "Message the recipient without the gift-giver's explicit permission.",
                "Call if wishlist or preferences already exist; suggest from those instead.",
                "Call if a pending preference request already exists for the same recipient and occasion.",
                "Guess or invent gift preferences — only use confirmed data.",
            ],
            important=[
                "ALWAYS ask the gift-giver permission before calling this tool.",
                "The recipient message is anonymous unless the gift-giver agrees to reveal identity.",
                "Duplicate requests are deduplicated — one pending request per recipient per occasion.",
                "The gift-giver will be notified when the recipient responds.",
            ],
            output="Preference request id, status, and expected notification timeline.",
        ),
        _params(
            {
                "recipient_phone": _str("Normalized phone number of the recipient (10-12 digits, country code)."),
                "occasion_id": _str("Optional occasion id this preference request is tied to. Helps scope the notification."),
                "anonymous": _bool("If true, do not reveal the gift-giver's identity to the recipient. Default true."),
                "permission_confirmed": _bool("True only after the gift-giver explicitly confirms Wishkr should message the recipient."),
            },
            required=["recipient_phone"],
        ),
    ),
}


def get_declaration(name: str) -> FunctionDeclaration:
    """Return the hand-written declaration for a tool name.

    Raises KeyError if the tool has no declaration — intentional, so that
    adding a Python wrapper without a matching schema fails fast at import.
    """
    return TOOL_DECLARATIONS[name]


# ─────────────────────────────────────────────────────────
# ToolSpec — unified contract layer
#
# Combines the Gemini FunctionDeclaration with the runtime contract metadata
# (requires / conditional_requires / produces / depends_on) that adapter.py
# uses for precondition validation.
#
# This module remains the single import surface for both the schema Gemini
# sees and the contract the adapter enforces.
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    """Unified contract for one tool: Gemini schema + graph metadata."""

    declaration: FunctionDeclaration
    requires: list[str] = field(default_factory=list)
    conditional_requires: dict[str, list[str]] = field(default_factory=dict)
    produces: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.declaration.name or ""


# ─────────────────────────────────────────────────────────
# Runtime contract metadata.
#
# Keeping this as a plain dict literal (not dataclasses) avoids noise; the
# shape is always validated by _build_tool_specs before TOOL_SPECS is
# exposed. Keys must stay 1:1 with TOOL_DECLARATIONS.
# ─────────────────────────────────────────────────────────

_TOOL_CONTRACTS: dict[str, dict] = {
    "get_user_profile": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["registration_state", "user_profile", "is_guest"],
        "depends_on": [],
    },
    "register_user": {
        "requires": ["name"],
        "conditional_requires": {},
        "produces": ["user_id", "registration_state", "city_id", "timezone"],
        "depends_on": ["get_user_profile"],
    },
    "add_occasion": {
        "requires": ["contact_name", "occasion_type", "date_day", "date_month"],
        "conditional_requires": {"occasion_type=CUSTOM": ["custom_label"]},
        "produces": ["occasion_id", "contact_id", "reminder_ids", "existing_occasion_id"],
        "depends_on": [],
    },
    "list_occasions": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["occasion_ids"],
        "depends_on": ["get_my_occasions"],
    },
    "get_my_occasions": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["occasion_ids", "contact_names", "occasion_dates"],
        "depends_on": [],
    },
    "get_occasion_details": {
        "requires": ["occasion_id"],
        "conditional_requires": {},
        "produces": [
            "occasion_details",
            "contact_id",
            "contact_city",
            "celebrant_occasion_id",
            "wishlist_urls",
            "recipient_wishlist_count",
            "giver_shortlist_count",
            "reminder_schedule",
            "gift_plan",
        ],
        "depends_on": ["get_my_occasions"],
    },
    "search_brands": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["brand_name", "hubble_product_id", "denomination_preview"],
        "depends_on": [],
    },
    "suggest_gifts": {
        "requires": [],
        "conditional_requires": {
            "scenario=self": ["self_preferences_checked"],
            "occasion_id_present": ["occasion_accessible"],
        },
        "produces": ["gift_suggestions", "brand_name", "provider", "already_taken"],
        "depends_on": ["get_self_preferences", "get_my_occasions", "get_occasion_details"],
    },
    "save_gift_plan": {
        "requires": ["occasion_id"],
        "conditional_requires": {
            "occasion_id_present": ["occasion_accessible"],
        },
        "produces": ["gift_plan"],
        "depends_on": ["get_my_occasions", "get_occasion_details"],
    },
    "select_gift_candidate": {
        "requires": ["provider_id", "candidate_id"],
        "conditional_requires": {},
        "produces": ["provider_id", "candidate_id", "name"],
        "depends_on": ["suggest_gifts"],
    },
    "create_checkout": {
        "requires": ["amount"],
        "conditional_requires": {
            "occasion_id_present": ["occasion_owned"],
            "celebrant_occasion_id": ["celebrant_friend_invited"],
            "duplicate_warning": ["duplicate_acknowledged_after_confirmation"],
        },
        "produces": ["order_id", "purchase_id", "payment_link", "manual_review"],
        "depends_on": ["select_gift_candidate", "get_brand_details"],
    },
    "get_self_preferences": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["preferences", "wishlists", "own_occasion_ids"],
        "depends_on": ["get_user_profile"],
    },
    "suggest_marketplace_gift_card": {
        "requires": ["marketplace"],
        "conditional_requires": {},
        "produces": ["marketplace_voucher_brand", "denomination_range"],
        "depends_on": [],
    },
    "get_brand_details": {
        "requires": ["brand_name"],
        "conditional_requires": {},
        "produces": ["denomination_type", "available_denominations", "min_amount", "max_amount", "hubble_product_id"],
        "depends_on": ["search_brands"],
    },
    "create_gift_order": {
        "requires": ["brand_name", "amount"],
        "conditional_requires": {
            "occasion_id_present": ["occasion_owned"],
            "duplicate_warning": ["duplicate_acknowledged_after_confirmation"],
            "fixed_denomination_brand": ["valid_denomination"],
        },
        "produces": ["order_id", "purchase_id", "payment_link", "payment_slip"],
        "depends_on": ["get_brand_details", "check_occasion_gifts"],
    },
    "list_orders": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["recent_orders"],
        "depends_on": [],
    },
    "check_occasion_gifts": {
        "requires": ["occasion_id"],
        "conditional_requires": {"caller": ["occasion_accessible_to_caller"]},
        "produces": ["categories_taken", "brands_taken", "privacy_note"],
        "depends_on": ["get_my_occasions", "get_celebrant_context"],
    },
    "log_external_purchase": {
        "requires": ["occasion_id", "item_description"],
        "conditional_requires": {"caller": ["occasion_accessible_to_caller"]},
        "produces": ["external_gift_claim_id", "dedupe_record"],
        "depends_on": ["check_occasion_gifts"],
    },
    "log_my_gift": {
        "requires": ["description"],
        "conditional_requires": {},
        "produces": ["gift_log_id", "contact_id"],
        "depends_on": [],
    },
    "add_to_wishlist": {
        "requires": ["url"],
        "conditional_requires": {
            "caller": ["purpose_matches_active_context"],
            "purpose=gift": ["recipient_occasion_id_present"],
        },
        "produces": ["wishlist_item_id", "saved_url"],
        "depends_on": ["get_celebrant_context", "get_my_occasions"],
    },
    "list_my_wishlist": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["items", "count"],
        "depends_on": [],
    },
    "get_friend_wishlist": {
        "requires": ["celebrant_occasion_id"],
        "conditional_requires": {"caller": ["celebrant_friend_invited"]},
        "produces": ["items", "count", "occasion_label"],
        "depends_on": ["get_celebrant_context"],
    },
    "reserve_wishlist_item": {
        "requires": ["wishlist_item_id", "celebrant_occasion_id"],
        "conditional_requires": {"caller": ["celebrant_friend_invited"]},
        "produces": ["reserved", "reservation_expires_at", "share_status"],
        "depends_on": ["get_friend_wishlist"],
    },
    "confirm_wishlist_purchase": {
        "requires": ["wishlist_item_id", "celebrant_occasion_id"],
        "conditional_requires": {"caller": ["celebrant_friend_invited"]},
        "produces": ["purchased", "share_status"],
        "depends_on": ["reserve_wishlist_item"],
    },
    "request_recipient_preferences": {
        "requires": ["recipient_phone"],
        "conditional_requires": {"caller": ["recipient_message_permission"]},
        "produces": ["request_id", "status"],
        "depends_on": [],
    },
    "remove_wishlist_item": {
        "requires": ["url"],
        "conditional_requires": {},
        "produces": ["success"],
        "depends_on": ["list_my_wishlist"],
    },
    "clear_wishlist": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["cleared"],
        "depends_on": [],
    },
    "get_upcoming_digest": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["upcoming_occasion_ids"],
        "depends_on": [],
    },
    "snooze_occasion": {
        "requires": ["occasion_id"],
        "conditional_requires": {"caller": ["occasion_owned"]},
        "produces": ["snooze_until"],
        "depends_on": ["get_my_occasions", "get_reminder_schedule"],
    },
    "skip_occasion_this_year": {
        "requires": ["occasion_id"],
        "conditional_requires": {"caller": ["occasion_owned"]},
        "produces": ["skipped_year"],
        "depends_on": ["get_my_occasions"],
    },
    "get_monthly_spend": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["monthly_spend_total"],
        "depends_on": [],
    },
    "create_batch_gift_orders": {
        "requires": ["items"],
        "conditional_requires": {
            "each_item": ["brand_name", "amount", "valid_denomination", "occasion_owned_if_present"],
        },
        "produces": ["order_ids", "payment_links", "per_item_results"],
        "depends_on": ["get_brand_details"],
    },
    "setup_celebrant_occasion": {
        "requires": ["occasion_type", "date_day", "date_month"],
        "conditional_requires": {
            "occasion_type=CUSTOM": ["custom_label"],
            "previous_add_to_wishlist_urls": ["wishlist_urls_passed_explicitly"],
        },
        "produces": ["occasion_id", "celebrant_occasion_id", "friend_phone_links", "reminder_ids"],
        "depends_on": [],
    },
    "update_celebrant_occasion": {
        "requires": ["occasion_id"],
        "conditional_requires": {
            "occasion_id_present": ["occasion_owned"],
        },
        "produces": [
            "occasion_id",
            "celebrant_occasion_id",
            "friend_phone_links",
            "reminder_ids",
        ],
        "depends_on": ["get_my_occasions", "setup_celebrant_occasion"],
    },
    "get_celebrant_context": {
        "requires": ["celebrant_occasion_id"],
        "conditional_requires": {"caller": ["celebrant_friend_invited"]},
        "produces": ["occasion_id", "preferences", "wishlists", "already_purchased"],
        "depends_on": ["setup_celebrant_occasion"],
    },
    "create_friend_gift_order": {
        "requires": ["celebrant_occasion_id", "brand_name", "amount", "buyer_name"],
        "conditional_requires": {
            "caller": ["celebrant_friend_invited"],
            "duplicate_warning": ["duplicate_acknowledged_after_confirmation"],
            "fixed_denomination_brand": ["valid_denomination"],
        },
        "produces": ["order_id", "purchase_id", "payment_link", "payment_slip"],
        "depends_on": ["get_celebrant_context", "get_brand_details"],
    },
    "edit_occasion": {
        "requires": ["occasion_id"],
        "conditional_requires": {
            "occasion_type=CUSTOM": ["custom_label"],
            "contact_name": ["occasion_has_contact"],
        },
        "produces": ["updated_occasion"],
        "depends_on": ["get_my_occasions", "add_occasion"],
    },
    "delete_occasion": {
        "requires": ["occasion_id"],
        "conditional_requires": {
            "caller": ["occasion_owned"],
            "confirmation": ["delete_confirmed"],
        },
        "produces": ["deleted_occasion_id", "cancelled_reminders"],
        "depends_on": ["get_my_occasions"],
    },
    "merge_occasions": {
        "requires": ["keep_occasion_id", "merge_occasion_id"],
        "conditional_requires": {},
        "produces": ["occasion_id", "merged_occasion_id"],
        "depends_on": ["get_my_occasions"],
    },
    "add_to_cart": {
        "requires": ["brand_name", "amount"],
        "conditional_requires": {
            "occasion_id_present": ["occasion_owned"],
            "brand": ["valid_denomination"],
        },
        "produces": ["cart_item"],
        "depends_on": ["get_brand_details", "get_my_occasions"],
    },
    "view_cart": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["cart_items", "cart_total"],
        "depends_on": [],
    },
    "checkout_cart": {
        "requires": [],
        "conditional_requires": {"cart": ["cart_nonempty_unexpired"]},
        "produces": ["order_ids", "payment_links", "per_item_results"],
        "depends_on": ["add_to_cart", "view_cart"],
    },
    "clear_cart": {
        "requires": [],
        "conditional_requires": {},
        "produces": ["cleared_count"],
        "depends_on": ["view_cart"],
    },
    "search_vendors": {
        "requires": ["category"],
        "conditional_requires": {
            "local_vendor_results": ["occasion_id", "occasion_has_contact", "contact_city_set"],
        },
        "produces": ["vendor_ids", "national_vendors", "local_vendors"],
        "depends_on": ["get_occasion_details", "set_contact_city"],
    },
    "get_reminder_schedule": {
        "requires": ["occasion_id"],
        "conditional_requires": {"caller": ["occasion_owned"]},
        "produces": ["reminder_ids", "scheduled_at", "target_phone", "target_name"],
        "depends_on": ["get_my_occasions"],
    },
    "get_reminder_history": {
        "requires": ["occasion_id"],
        "conditional_requires": {"caller": ["occasion_owned"]},
        "produces": ["pending_reminders", "sent_reminders", "other_reminders"],
        "depends_on": ["get_my_occasions"],
    },
    "set_reminder_schedule": {
        "requires": ["occasion_id", "days_before"],
        "conditional_requires": {"caller": ["occasion_owned"]},
        "produces": ["reminder_ids", "scheduled_at", "days_before"],
        "depends_on": ["get_my_occasions", "get_reminder_schedule"],
    },
    "send_reminder_now": {
        "requires": ["occasion_id"],
        "conditional_requires": {
            "target_phone_present": ["celebrant_occasion_exists", "target_phone_linked_to_friend"],
        },
        "produces": ["sent_reminder_id", "sent_to", "whatsapp_msg_id"],
        "depends_on": ["setup_celebrant_occasion", "get_my_occasions", "get_reminder_schedule"],
    },
    "set_contact_city": {
        "requires": ["contact_id", "city"],
        "conditional_requires": {
            "contact_id": ["contact_owned"],
        },
        "produces": ["contact_id", "city_id", "city_raw"],
        "depends_on": ["get_occasion_details"],
    },
}


def _build_tool_specs() -> dict[str, ToolSpec]:
    graph = _TOOL_CONTRACTS
    decl_names = set(TOOL_DECLARATIONS)
    graph_names = set(graph)

    missing_in_graph = decl_names - graph_names
    if missing_in_graph:
        raise RuntimeError(
            f"TOOL_DECLARATIONS has no ToolSpec metadata for: {sorted(missing_in_graph)}"
        )
    missing_in_decls = graph_names - decl_names
    if missing_in_decls:
        raise RuntimeError(
            f"ToolSpec metadata has no TOOL_DECLARATIONS entry for: {sorted(missing_in_decls)}"
        )

    specs: dict[str, ToolSpec] = {}
    for name, decl in TOOL_DECLARATIONS.items():
        meta = graph[name]
        param_fields: set[str] = set(
            (decl.parameters.properties or {}).keys()
        ) if decl.parameters else set()

        for req_field in meta.get("requires", []):
            if req_field not in param_fields:
                raise RuntimeError(
                    f"{name}: requires field '{req_field}' not in parameter schema"
                )

        for cond_key in meta.get("conditional_requires", {}):
            if "=" in cond_key:
                field_name = cond_key.split("=", 1)[0]
                if field_name not in param_fields:
                    raise RuntimeError(
                        f"{name}: conditional_requires key '{cond_key}' references "
                        f"unknown field '{field_name}'"
                    )

        for dep_tool in meta.get("depends_on", []):
            if dep_tool not in decl_names:
                raise RuntimeError(
                    f"{name}: depends_on '{dep_tool}' is not in TOOL_DECLARATIONS"
                )

        specs[name] = ToolSpec(
            declaration=decl,
            requires=list(meta.get("requires", [])),
            conditional_requires={
                k: list(v) for k, v in meta.get("conditional_requires", {}).items()
            },
            produces=list(meta.get("produces", [])),
            depends_on=list(meta.get("depends_on", [])),
        )
    return specs


TOOL_SPECS: dict[str, ToolSpec] = _build_tool_specs()


def get_tool_spec(name: str) -> ToolSpec:
    """Return the unified ToolSpec for a tool name.

    Raises KeyError if the tool is not registered — intentional, callers
    should not silently fall through on typos.
    """
    return TOOL_SPECS[name]
