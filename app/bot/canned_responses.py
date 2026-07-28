"""Hardcoded replies for intents that don't need the main LLM.

Short-circuiting these saves ~500ms + ~2k tokens per matching turn AND
eliminates a class of prompt-obedience bugs (menu dumps, product-loop
hallucination) at the source.
"""

MENU_RESPONSE = """Here's the menu:

1. *Add occasion* — track someone's birthday/anniversary
2. *Gift ideas* — informational suggestions
3. *My occasions* — upcoming occasions
4. *Reminder schedule* — see upcoming nudges
5. *My occasion setup* — tell friends what YOU want

What would you like to do?"""


PRODUCT_GATE_RESPONSE = (
    "Heads up — Wishkr is reminder-first on WhatsApp right now.\n\n"
    "Best option: paste the product link and I'll add it to your *wishlist* so gifters see exactly what you want.\n\n"
    "For now, you can buy from your preferred store."
)


PURCHASE_DISABLED_RESPONSE = (
    "I can help you remember the occasion and suggest gift ideas. "
    "For now, you can buy from your preferred store."
)


WISHLIST_AFFIRMATIVE_REPROMPT = (
    "Cool 👍 Paste the *product link* (Wishkr, Amazon, Flipkart, Croma, etc.) "
    "and I'll add it to your wishlist."
)

WISHLIST_ADDED_RESPONSE = "Added to wishlist 👍"

WISHLIST_DUPLICATE_RESPONSE = "Already had this 👍"

WISHLIST_LIMIT_RESPONSE = "Your wishlist is full (50 items). Remove one to add more."

WISHLIST_EMPTY_RESPONSE = (
    "Your wishlist is empty 🤔\n"
    "Send me a product link (Wishkr, Amazon, Flipkart, Croma, etc.) "
    "and I'll add it."
)


def format_wishlist(items: list[tuple[str, str]]) -> str:
    """Render `[(title, url), ...]` as a WhatsApp-friendly numbered list."""
    if not items:
        return WISHLIST_EMPTY_RESPONSE
    header = f"Your wishlist ({len(items)} item{'s' if len(items) != 1 else ''}):\n"
    lines = []
    for i, (title, url) in enumerate(items, start=1):
        label = (title or "").strip() or url
        if title and label != url:
            lines.append(f"{i}. *{label}*\n   {url}")
        else:
            lines.append(f"{i}. {url}")
    return header + "\n".join(lines)
