"""/product/quote is unauthenticated and does one DB lookup per line, so it is
throttled per IP. These cover the limiter itself and the IP extraction it keys
on — a limiter that resolves every caller to the same key would throttle all
shoppers together, and one that resolves each to a unique key throttles nobody.
"""

import pytest

from app.api.product_orders import _client_ip, _quote_rate_limiter


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None, host: str | None = None) -> None:
        self.headers = headers or {}
        self.client = _FakeClient(host) if host else None


def test_client_ip_prefers_forwarded_header():
    # Behind Vercel/Cloudflare the socket peer is the proxy, so the real client
    # is the FIRST entry of x-forwarded-for.
    request = _FakeRequest({"x-forwarded-for": "203.0.113.9, 70.41.3.18"}, host="10.0.0.1")
    assert _client_ip(request) == "203.0.113.9"


def test_client_ip_falls_back_to_peer_then_unknown():
    assert _client_ip(_FakeRequest(host="198.51.100.7")) == "198.51.100.7"
    assert _client_ip(_FakeRequest()) == "unknown"


@pytest.mark.asyncio
async def test_quote_limiter_blocks_only_the_noisy_ip():
    limit = _quote_rate_limiter._max_requests
    noisy = "203.0.113.100"
    quiet = "203.0.113.200"

    for _ in range(limit):
        assert await _quote_rate_limiter.allow(noisy) is True

    # Over budget: this caller is cut off...
    assert await _quote_rate_limiter.allow(noisy) is False
    # ...while everyone else still shops normally.
    assert await _quote_rate_limiter.allow(quiet) is True
