"""Drop the storefront product cache after any successful catalog write.

The cached ``/api/catalog/products`` payload is derived from products, their
variants and addons, their vendor, and the storefront pricing settings — all of
which are mutated from ``/api/admin/*`` and the vendor self-service edit routes,
across ~24 separate commit sites.

Hooking each of those by hand would work until someone adds the 25th and forgets,
and the failure is silent: the storefront just serves a stale price. So this
mirrors ``audit_admin_writes`` and covers the whole surface at the middleware
layer — any new write route is handled automatically.

Cache invalidation is best-effort: the short TTL on the entry bounds staleness
even if Redis is down, so a failure here must never break the write itself.
"""

from starlette.requests import Request
from starlette.responses import Response

from app.api.catalog import invalidate_product_cache

_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

# Prefixes whose writes can change what /catalog/products returns. `/admin/`
# covers products, variants, addons, vendors and pricing settings;
# `/vendor-edit/` is the vendor self-service editor.
_CATALOG_WRITE_PREFIXES = ("/api/admin/", "/api/vendor-edit/")


async def invalidate_catalog_cache(request: Request, call_next):
    response: Response = await call_next(request)

    if request.method not in _MUTATING_METHODS:
        return response
    if not request.url.path.startswith(_CATALOG_WRITE_PREFIXES):
        return response
    # Only a write that actually succeeded can have changed the catalogue.
    if response.status_code >= 400:
        return response

    # Never let a cache problem surface as a failed admin write.
    try:
        await invalidate_product_cache()
    except Exception:
        pass

    return response
