from app.utils.html_sanitize import sanitize_html


def test_passes_through_clean_html():
    html = "<p>Hello <strong>world</strong> &amp; more</p>"
    assert sanitize_html(html) == html


def test_recovers_double_escaped_html():
    # Rich HTML pasted into TipTap gets escaped, then wrapped — stored as
    # visible "<p>" text on the storefront. The sanitiser must decode it back
    # to real tags. (See the Customized Candle Gift Hampers prod incident.)
    corrupted = "<p>&lt;p&gt;Hi &lt;strong&gt;there&lt;/strong&gt;&lt;/p&gt;</p>"
    out = sanitize_html(corrupted)
    assert "&lt;" not in out
    assert "<strong>there</strong>" in out


def test_leaves_plain_less_than_alone():
    # A bare "&lt;" not followed by a tag name is a real comparison, not markup.
    text = "Use 5 &lt; 10 in your note"
    assert sanitize_html(text) == text


def test_strips_disallowed_tags_keeps_text():
    # Disallowed tags are dropped but their text content is preserved, so the
    # <script> element itself can never reach the DOM.
    out = sanitize_html("<script>alert(1)</script>hi")
    assert "<script>" not in out
    assert out == "alert(1)hi"
