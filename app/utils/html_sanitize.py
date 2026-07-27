"""Minimal allowlist HTML sanitiser for rich product descriptions.

The description is authored in a constrained TipTap editor (StarterKit), which
only emits a small, known tag set. We still sanitise server-side as the real
security boundary — never trust client HTML — using a strict allowlist with no
attributes except safe `href` on links. Anything else (scripts, event handlers,
style, iframes, unknown tags) is dropped.

Dependency-free on purpose: the allowed grammar is tiny, so a stdlib HTMLParser
is sufficient and avoids adding bleach/nh3 to the production image.
"""

import re
from html.parser import HTMLParser
from html import escape, unescape

# Tags the rich editor can produce, plus inline formatting. Everything else is
# stripped (its text content is kept).
_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s",
    "ul", "ol", "li", "blockquote", "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "a",
}
_VOID_TAGS = {"br"}
# Only safe hrefs on <a>. No javascript:, data:, etc.
_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "/")


# Matches an entity-escaped opening/closing tag of an allowed name, e.g.
# "&lt;p&gt;" or "&lt;/strong&gt;". Used to detect descriptions that were
# double-escaped upstream (rich HTML pasted into TipTap, which escapes markup,
# then saved). When present we decode one entity layer so the sanitiser sees
# real tags instead of storing them as visible text. A stray "&lt;" not
# followed by a known tag name (e.g. "5 &lt; 10") is left untouched.
_ESCAPED_TAG_RE = re.compile(
    r"&lt;\s*/?\s*(?:p|br|strong|b|em|i|u|s|ul|ol|li|blockquote|code|pre|h[1-6]|a)\b",
    re.IGNORECASE,
)


def _recover_double_escaped(value: str) -> str:
    if _ESCAPED_TAG_RE.search(value):
        return unescape(value)
    return value


def _safe_href(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v.lower().startswith(_SAFE_URL_SCHEMES) or v.startswith("#"):
        return v
    return None


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href:
                self.out.append(f'<a href="{escape(href)}" rel="nofollow noopener" target="_blank">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _ALLOWED_TAGS and tag in _VOID_TAGS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        # Close down to the matching open tag, keeping nesting balanced.
        if tag in self._open:
            while self._open:
                open_tag = self._open.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        self.out.append(escape(data))

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize_html(value: str | None) -> str | None:
    """Return allowlist-sanitised HTML, or None/empty unchanged."""
    if not value:
        return value
    value = _recover_double_escaped(value)
    parser = _Sanitizer()
    parser.feed(value)
    parser.close()
    return parser.result()


def html_to_text(value: str | None) -> str | None:
    """Strip all tags to plain text — for SEO meta descriptions / structured data
    where HTML must not leak into snippets."""
    if not value:
        return value

    class _Strip(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    s = _Strip()
    s.feed(value)
    s.close()
    return " ".join("".join(s.parts).split())
