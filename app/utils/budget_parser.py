"""Parse free-form budget strings into (min, max) rupee tuples.

Handles Indian-conversation patterns: 'k' suffix, ranges, 'around X', 'under X', etc.
Returns None for either bound when the user didn't specify it.

Used by suggest_gifts (and any future tool that takes a budget) to pre-process
raw budget strings before passing into ranker.
"""

from __future__ import annotations

import re

# Tunable: how far around X "around X" stretches (±30%)
_AROUND_FUZZ = 0.3

# Reject as ambiguous if parsed max exceeds this — likely user meant '1k' not '1 lakh'
_MAX_REASONABLE_RUPEES = 100_000

# k → 1000 multiplier; lakh → 100_000
_SUFFIX_MULT = {"k": 1000, "K": 1000, "lakh": 100_000, "lac": 100_000, "l": 100_000}
_AMOUNT_TOKEN = (
    r"(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|lakh|lac|l)?"
)
_NON_BUDGET_UNIT = re.compile(
    r"^\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?"
    r"|years?|kids?|children|people|persons?|items?|pieces?|units?)\b",
    re.IGNORECASE,
)


def _to_rupees(token: str) -> int | None:
    """'1k' → 1000, '1500' → 1500, '1.5k' → 1500. None if unparseable."""
    token = token.strip().lower().replace(",", "").replace("₹", "").replace("rs", "").replace("inr", "").strip()
    if not token:
        return None
    # Suffix?
    for suf, mult in _SUFFIX_MULT.items():
        if token.endswith(suf):
            num = token[:-len(suf)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                return None
    # Plain number
    try:
        return int(float(token))
    except ValueError:
        return None


def _matched_rupees(match: re.Match[str], group: int = 1) -> int | None:
    """Parse a captured amount unless it is immediately a non-money count."""
    if _NON_BUDGET_UNIT.match(match.string[match.end(group):]):
        return None
    return _to_rupees(match.group(group))


def parse_budget(text: str) -> tuple[int | None, int | None]:
    """Returns (min_rupees, max_rupees). Either may be None.

    Examples:
        '1000'              → (1000, 1000)
        '1k'                → (1000, 1000)
        '1000-3000'         → (1000, 3000)
        '1k-3k'             → (1000, 3000)
        '1000 to 3000'      → (1000, 3000)
        'around 1500'       → (1050, 1950)
        'under 2000'        → (None, 2000)
        'max 3k'            → (None, 3000)
        '500 onwards'       → (500, None)
        '1000k to 3000k'    → (1000, 3000) and clamped (k applied but result reasonable)
        '1 lakh'            → (100000, 100000)
    """
    if not text or not text.strip():
        return (None, None)

    s = text.lower().strip()

    # "around X" / "approx X" / "near X"
    if m := re.match(r"^(?:around|approx|approximately|near|about|roughly)\s+(.+)$", s):
        val = _to_rupees(m.group(1))
        if val is None:
            return (None, None)
        return (int(val * (1 - _AROUND_FUZZ)), int(val * (1 + _AROUND_FUZZ)))

    # "under X" / "max X" / "no more than X" / "nothing over X"
    if m := re.match(
        r"^(?:under|below|max(?:imum)?(?:\s+of)?|upto|up to|less than"
        r"|no more than|not more than|nothing over|<=?)\s+(.+)$",
        s,
    ):
        val = _to_rupees(m.group(1))
        return (None, val) if val else (None, None)

    # "X onwards" / "starting X" / "min X" / "above X" / "more than X"
    if m := re.match(r"^(?:starting(?:\s+at)?|min(?:imum)?|above|over|more than|>=?)\s+(.+)$", s):
        val = _to_rupees(m.group(1))
        return (val, None) if val else (None, None)
    if m := re.match(r"^(.+?)\s+onwards?$", s):
        val = _to_rupees(m.group(1))
        return (val, None) if val else (None, None)

    # Range: "X to Y", "X and Y", "X-Y", "X – Y", "between X and Y"
    if m := re.match(r"^between\s+(.+?)\s+and\s+(.+)$", s):
        lo, hi = _to_rupees(m.group(1)), _to_rupees(m.group(2))
        return _normalise_range(lo, hi)
    if m := re.match(
        rf"^({_AMOUNT_TOKEN})"
        rf"(?:\s+(?:to|and|until|till)\s+|\s*[-–—]\s*)"
        rf"({_AMOUNT_TOKEN})$",
        s,
    ):
        lo, hi = _to_rupees(m.group(1)), _to_rupees(m.group(2))
        return _normalise_range(lo, hi)

    # Natural conversational sentences. Keep this signal-gated so dates,
    # order numbers, and phone numbers embedded in unrelated text are not
    # mistaken for budgets.
    range_signal = re.search(
        rf"\b(?:budget|spend|price|cost|range)\b.*?"
        rf"({_AMOUNT_TOKEN})(?:\s+(?:to|and|until|till)\s+|\s*[-–—]\s*)"
        rf"({_AMOUNT_TOKEN})",
        s,
    )
    if range_signal:
        lo = _matched_rupees(range_signal, 1)
        hi = _matched_rupees(range_signal, 2)
        if lo is None or hi is None:
            return (None, None)
        return _normalise_range(lo, hi)

    upper_signal = re.search(
        rf"(?:\b(?:under|below|upto|up\s+to|less\s+than|no\s+more\s+than"
        rf"|not\s+more\s+than|nothing\s+over)\b"
        rf"|\bmax(?:imum)?(?:\s+budget)?(?:\s+of|\s+is)?\b)"
        rf"\s*[:=-]?\s*({_AMOUNT_TOKEN})",
        s,
    )
    if upper_signal:
        val = _matched_rupees(upper_signal)
        return (None, val) if val else (None, None)

    spend_up_to = re.search(
        rf"\b(?:i\s+can\s+)?spend(?:ing)?\s+(?:up\s+to|max(?:imum)?(?:\s+of)?)\s+"
        rf"({_AMOUNT_TOKEN})",
        s,
    )
    if spend_up_to:
        val = _matched_rupees(spend_up_to)
        return (None, val) if val else (None, None)

    exact_signal = re.search(
        rf"(?:\b(?:my\s+)?budget(?:\s+is|\s+of)?\b"
        rf"|\b(?:i\s+can\s+)?spend\b"
        rf"|\b(?:i\s+)?(?:just\s+)?told(?:\s+you)?\b)"
        rf"\s*[:=-]?\s*({_AMOUNT_TOKEN})",
        s,
    )
    if exact_signal:
        val = _matched_rupees(exact_signal)
        return _normalise_range(val, val)

    # Plain single value — route through _normalise_range so the
    # implausibly-large guard (_MAX_REASONABLE_RUPEES) catches stray phone
    # numbers and other oversized integers.
    val = _to_rupees(s)
    if val is not None:
        return _normalise_range(val, val)

    return (None, None)


def parse_budget_hint(text: str | None) -> tuple[int | None, int | None]:
    """Parse the compact price ranges used by deterministic and AI gift ideas."""
    normalized = re.sub(
        r"^(?:around|approx(?:imately)?|near|about|roughly)\s+",
        "",
        (text or "").strip(),
        flags=re.IGNORECASE,
    )
    return parse_budget(normalized)


def budget_ranges_overlap(
    item_min: int | None,
    item_max: int | None,
    budget_min: int | None,
    budget_max: int | None,
) -> bool:
    """Return whether a priced item/idea intersects the saved budget range."""
    if budget_min is not None and item_max is not None and item_max < budget_min:
        return False
    if budget_max is not None and item_min is not None and item_min > budget_max:
        return False
    return True


def _normalise_range(lo: int | None, hi: int | None) -> tuple[int | None, int | None]:
    """Sanity-check a parsed (min, max) pair."""
    if lo is None and hi is None:
        return (None, None)
    # Swap if user wrote them backwards
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    # Reject implausibly large values (likely '1000k' meaning 1000, not 1M)
    if hi is not None and hi > _MAX_REASONABLE_RUPEES:
        return (None, None)
    if lo is not None and lo > _MAX_REASONABLE_RUPEES:
        return (None, None)
    return (lo, hi)
