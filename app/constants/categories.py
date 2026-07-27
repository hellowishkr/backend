"""Canonical partner-intake categories.

Shared by the partner form validator and (eventually) the admin triage UI.
Vendor catalog taxonomy is separate — see vendors.category.
"""

# Lowercase, space-separated. Keep in sync with PartnerForm.CATEGORIES.
PARTNER_CATEGORIES: frozenset[str] = frozenset({
    "flowers",
    "cakes",
    "chocolates",
    "decorations",
    "personalized gifts",
    "hampers",
    "experiences",
})

# Sentinel that flips on the "Other — please specify" input on the form.
# Replaced at ingestion with the user-typed value (lowercased, trimmed).
OTHER_CATEGORY_SENTINEL = "other"
