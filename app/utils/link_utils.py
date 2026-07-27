"""URL detection & normalization for wishlist links.

Pure functions, no network calls. Keep this file fast (<1ms per call) — it
runs inline during message handling.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse, urlunparse, urlencode

# Commerce domains Wishkr recognizes as product links. Deliberately narrow to
# avoid false positives on blogs, social media, news.
# Hosts the LLM wishlist tool should reject outright — social, video,
# news, encyclopedia, search. Permissive elsewhere; assume any other
# `https://` host is a plausible product page.
_DISALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.com",
    "twitter.com", "x.com", "tiktok.com", "linkedin.com", "reddit.com",
    "wikipedia.org", "google.com", "bing.com", "duckduckgo.com",
    "medium.com", "substack.com", "pinterest.com", "snapchat.com",
)

_PRODUCT_DOMAINS = (
    "amazon.",
    "amzn.",
    "flipkart.",
    "fkrt.",
    "croma.com",
    "myntra.com",
    "ajio.com",
    "tatacliq.com",
    "reliancedigital.",
    "nykaa.com",
    "meesho.com",
    "snapdeal.com",
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Scheme-less commerce URLs: `amazon.in/...`, `www.flipkart.com/...`, etc.
# Matches host + path so we can prepend https:// and reuse existing pipeline.
_SCHEMELESS_URL_RE = re.compile(
    r"(?<![@\w.])"
    r"(?:www\.)?"
    r"(?:amazon\.[a-z.]{2,6}|amzn\.to|flipkart\.com|fkrt\.[a-z.]+|"
    r"croma\.com|myntra\.com|ajio\.com|tatacliq\.com|"
    r"reliancedigital\.[a-z.]+|nykaa\.com|meesho\.com|snapdeal\.com)"
    r"/[^\s<>\"']+",
    re.IGNORECASE,
)

# Amazon ASIN appears as `/dp/XXXXXXXXXX` or `/gp/product/XXXXXXXXXX`
_AMAZON_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)
# Bare ASIN path (no host) — e.g. when user pastes only the path portion
# of an Amazon URL: `Something-Headlights/dp/B0F3G43ZN1/ref=...`
_BARE_ASIN_RE = re.compile(r"(?:^|[\s/])(?:dp|gp/product)/([A-Z0-9]{10})", re.IGNORECASE)
# Flipkart product id — last path segment often `/p/itm<hex>` or query `?pid=...`
_FLIPKART_PID_RE = re.compile(r"/p/(itm[a-z0-9]+)", re.IGNORECASE)

# Tracking parameters to strip for canonicalization
_TRACKING_PARAMS = frozenset({
    "ref", "ref_", "tag", "linkcode", "camp", "creative", "creativeasin",
    "psc", "th", "pf_rd_p", "pf_rd_r", "pf_rd_s", "pf_rd_t", "pf_rd_i",
    "sprefix", "crid", "keywords", "qid", "sr",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "pid", "lid", "marketplace", "store",  # Flipkart
    "_encoding", "smid",
})


def extract_urls(text: str) -> list[str]:
    """Return all URLs found in `text`, preserving order.

    Catches three forms users actually paste:
      1. Full URLs: `https://amazon.in/dp/B0X...`
      2. Scheme-less: `amazon.in/dp/B0X...`, `www.flipkart.com/...`
      3. Bare Amazon path: `Something/dp/B0F3G43ZN1/ref=...`
         (synthesized to `https://amazon.in/dp/B0F3G43ZN1`)
    """
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        cleaned = url.rstrip(".,;:!?)]}>\"'")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)

    for u in _URL_RE.findall(text):
        _add(u)

    for u in _SCHEMELESS_URL_RE.findall(text):
        _add(f"https://{u}")

    if not out:
        m = _BARE_ASIN_RE.search(text)
        if m:
            _add(f"https://amazon.in/dp/{m.group(1).upper()}")

    return out


def is_product_link(url: str) -> bool:
    """True if the URL's host looks like a known commerce site."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(d in host for d in _PRODUCT_DOMAINS)


def is_disallowed_link(url: str) -> bool:
    """True if URL host is on the social/video/news deny-list.

    Used by the LLM wishlist tool to reject obvious non-product pastes
    (e.g. an Instagram reel) without needing an explicit allow-list.
    """
    if not url:
        return True
    host = (urlparse(url).hostname or "").lower().lstrip("www.")
    if not host:
        return True
    return any(host == d or host.endswith("." + d) for d in _DISALLOWED_HOSTS)


def normalize_url(url: str) -> str:
    """Strip tracking params; lowercase host; drop fragment.

    Idempotent — calling twice gives the same result.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    # Keep port if present (rare for commerce)
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    kept_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(kept_params)
    # Drop trailing slash for consistency
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", netloc, path, "", query, ""))


def extract_product_id(url: str) -> str | None:
    """Best-effort extraction of a provider-native product identifier.

    Returns e.g. 'amazon:B09XYZ1234' or 'flipkart:itm8ab…', or None.
    """
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if "amazon." in host or "amzn." in host:
        m = _AMAZON_ASIN_RE.search(url)
        if m:
            return f"amazon:{m.group(1).upper()}"
    if "flipkart." in host or "fkrt." in host:
        m = _FLIPKART_PID_RE.search(url)
        if m:
            return f"flipkart:{m.group(1).lower()}"
    return None


_ALNUM = re.compile(r"[A-Za-z0-9\u0900-\u0FFF]")


def sole_product_link_url(text: str) -> str | None:
    """If `text` is essentially a single product URL (optional tiny filler), return it.

    Used to handle bare link pastes and repeat sends without invoking the LLM.
    """
    urls = extract_urls(text)
    if len(urls) != 1:
        return None
    u = urls[0]
    if not is_product_link(u):
        return None
    rest = text.replace(u, "", 1).strip()
    if len(_ALNUM.findall(rest)) > 15:
        return None
    return u


def guess_product_name(url: str) -> str:
    """Heuristic name from URL slug. Not guaranteed accurate — UX nicety only."""
    if not url:
        return ""
    path = urlparse(url).path
    # Find the longest hyphenated segment — usually the product-title slug
    segments = [s for s in path.split("/") if s and "-" in s]
    if not segments:
        return ""
    slug = max(segments, key=len)
    words = re.sub(r"[-_]+", " ", slug).strip()
    # Drop numeric-only tokens and known provider identifiers
    cleaned = " ".join(
        w for w in words.split()
        if not w.isdigit() and not re.match(r"^(itm|dp|gp|[A-Z0-9]{10})$", w, re.IGNORECASE)
    )
    return cleaned[:60].strip()
