"""Storefront product cache: routing rules for invalidation.

The failure this guards is silent — a missed invalidation just serves a stale
price until the TTL expires, with no error anywhere. So the middleware's
decision table is asserted directly.
"""

import pytest

from app.api import catalog
from app.api.catalog_cache_middleware import invalidate_catalog_cache


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.url = _FakeURL(path)


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


async def _run(monkeypatch, method: str, path: str, status: int = 200) -> bool:
    """Return True if the middleware invalidated the cache for this request."""
    called = False

    async def _fake_invalidate() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        "app.api.catalog_cache_middleware.invalidate_product_cache", _fake_invalidate
    )

    async def _call_next(_request):
        return _FakeResponse(status)

    await invalidate_catalog_cache(_FakeRequest(method, path), _call_next)
    return called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/admin/catalog/products"),
        ("PATCH", "/api/admin/catalog/products/prd_1"),
        ("DELETE", "/api/admin/catalog/products/prd_1"),
        ("PATCH", "/api/admin/catalog/vendors/ven_1"),
        # Pricing settings change the multipliers embedded in the payload.
        ("PATCH", "/api/admin/pricing"),
        # Vendor self-service edits are a second write surface.
        ("POST", "/api/vendor-edit/tok_abc/products"),
    ],
)
async def test_catalog_writes_invalidate(monkeypatch, method, path):
    assert await _run(monkeypatch, method, path) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,status",
    [
        # Reads never invalidate.
        ("GET", "/api/admin/catalog/products", 200),
        # Non-catalog writes are irrelevant to this cache.
        ("POST", "/api/orders/product/quote", 200),
        ("POST", "/api/feedback", 200),
        # A rejected write changed nothing, so the cache is still valid.
        ("PATCH", "/api/admin/catalog/products/prd_1", 422),
        ("POST", "/api/admin/catalog/products", 500),
    ],
)
async def test_non_catalog_or_failed_writes_do_not_invalidate(monkeypatch, method, path, status):
    assert await _run(monkeypatch, method, path, status) is False


@pytest.mark.asyncio
async def test_invalidate_is_noop_without_redis(monkeypatch):
    # Local dev and tests run without Redis; invalidation must stay silent
    # rather than raising into the caller's write path.
    monkeypatch.setattr("app.api.catalog.get_redis", lambda: None)
    await catalog.invalidate_product_cache()


@pytest.mark.asyncio
async def test_invalidate_swallows_redis_errors(monkeypatch):
    from redis.exceptions import RedisError

    class _BrokenRedis:
        async def delete(self, *_args):
            raise RedisError("connection lost")

    monkeypatch.setattr("app.api.catalog.get_redis", lambda: _BrokenRedis())
    # A degraded Redis must not break an admin write; the TTL still bounds
    # staleness.
    await catalog.invalidate_product_cache()


def test_products_cache_ttl_is_short():
    # The payload embeds storefront pricing multipliers, so a long TTL would
    # show stale prices whenever an invalidation is missed.
    assert catalog.PRODUCTS_CACHE_TTL_SECONDS <= 60
