import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings


# Concurrency cap on outbound Hubble calls. Hubble doesn't publish a hard
# rate limit, but unbounded fan-out from many simultaneous orders during a
# burst can return 429 / time-out and degrade voucher issue. Ten concurrent
# requests is well below any partner ceiling and lets a normal launch-day
# spike (~5 orders/min sustained) breathe without queueing.
_HUBBLE_CONCURRENCY = 10
_hubble_semaphore = asyncio.Semaphore(_HUBBLE_CONCURRENCY)
logger = logging.getLogger("wishkr.services.hubble")


def integration_unavailable_response(reason: str) -> dict[str, str]:
    return {
        "status": "integration_unavailable",
        "provider": "hubble",
        "reason": reason,
    }


class HubbleUnavailableError(Exception):
    """Raised when Hubble is intentionally unavailable or auth failed safely."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.response = integration_unavailable_response(reason)


class HubbleService:
    """Hubble Money API client — auth, catalogue, orders."""

    def __init__(self):
        self._token: str | None = None
        self._token_expires_at: float = 0
        self._brands_cache: list[dict] = []
        self._brands_cached_at: float = 0
        self._unavailable_reason: str | None = None
        self._client = httpx.AsyncClient(base_url=settings.hubble_effective_base_url, timeout=30)

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def mark_unavailable(self, reason: str) -> None:
        self._token = None
        self._token_expires_at = 0
        self._unavailable_reason = reason

    def mark_available(self) -> None:
        self._unavailable_reason = None

    def unavailable_response(self) -> dict[str, str]:
        return integration_unavailable_response(self._unavailable_reason or "Hubble integration is unavailable")

    def _enabled_or_raise(self) -> None:
        if not settings.hubble_enabled:
            self.mark_unavailable("Hubble integration is disabled")
            raise HubbleUnavailableError(self._unavailable_reason or "Hubble integration is disabled")
        if self._unavailable_reason:
            raise HubbleUnavailableError(self._unavailable_reason)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "X-REQUEST-ID": f"wishkr-{int(time.time())}",
        }

    # ─── AUTH ──────────────────────────────────────────────

    async def authenticate(self) -> str:
        """Get auth token. Auto-refreshes if within 1 hour of expiry."""
        self._enabled_or_raise()

        if self._token and time.time() < (self._token_expires_at - 3600):
            return self._token

        try:
            resp = await self._client.post(
                "/v1/partners/auth/login",
                json={
                    "clientId": settings.hubble_effective_client_id,
                    "clientSecret": settings.hubble_effective_client_secret,
                },
                headers={"Content-Type": "application/json", "X-REQUEST-ID": f"wishkr-auth-{int(time.time())}"},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            reason = f"Hubble auth failed with HTTP {e.response.status_code}"
            self.mark_unavailable(reason)
            logger.warning(
                "Hubble auth unavailable env=%s status_code=%s",
                settings.hubble_env,
                e.response.status_code,
            )
            raise HubbleUnavailableError(reason) from e
        except httpx.RequestError as e:
            reason = "Hubble auth request failed"
            self.mark_unavailable(reason)
            logger.warning("Hubble auth request failed env=%s error=%s", settings.hubble_env, type(e).__name__)
            raise HubbleUnavailableError(reason) from e
        data = resp.json()

        self._token = str(data["token"])
        self._token_expires_at = time.time() + data["expiresInSecs"]
        self.mark_available()
        return self._token

    async def _ensure_auth(self):
        """Ensure we have a valid token before making API calls."""
        await self.authenticate()

    # ─── CATALOGUE ─────────────────────────────────────────

    async def get_brands(self, force_refresh: bool = False) -> list[dict]:
        """Fetch all active brands. Cached for 6 hours."""
        cache_ttl = 6 * 3600
        if self._brands_cache and not force_refresh and (time.time() - self._brands_cached_at) < cache_ttl:
            return self._brands_cache

        await self._ensure_auth()

        all_brands: list[dict] = []
        page = 1
        page_size = 100
        while page <= 50:  # hard cap to prevent runaway loops
            resp = await self._client.get(
                "/v1/partners/products",
                params={"limit": page_size, "pageNo": page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            brands = data.get("data", [])
            all_brands.extend(brands)

            # Hubble returns a non-null nextCursor even on the last page,
            # so stop when the page is under-full or empty.
            if len(brands) < page_size:
                break
            page += 1

        # Only keep active brands
        self._brands_cache = [b for b in all_brands if b.get("status") == "ACTIVE"]
        self._brands_cached_at = time.time()
        return self._brands_cache

    async def get_brand(self, product_id: str) -> dict | None:
        """Get a single brand by product ID."""
        await self._ensure_auth()
        resp = await self._client.get(
            f"/v1/partners/products/{product_id}",
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ─── ORDERS ────────────────────────────────────────────

    async def create_order(
        self,
        product_id: str,
        amount: int,
        denomination: int,
        quantity: int,
        reference_id: str,
        customer_name: str,
        customer_phone: str,
        customer_email: str = "",
    ) -> dict[str, Any]:
        """
        Place a gift card order. Returns order with voucher details.

        Retries 3x with exponential backoff on failure.
        """
        await self._ensure_auth()

        payload = {
            "productId": product_id,
            "referenceId": reference_id,
            "amount": amount,
            "denominationDetails": [{"denomination": denomination, "quantity": quantity}],
            "customerDetails": {
                "name": customer_name,
                "phoneNumber": customer_phone,
                "email": customer_email,
            },
        }

        last_error = None
        # Cap fan-out so a launch-day burst can't pile arbitrarily many
        # in-flight requests at Hubble (or starve the local connection pool).
        async with _hubble_semaphore:
            for attempt in range(3):
                try:
                    resp = await self._client.post(
                        "/v1/partners/orders",
                        json=payload,
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("status") == "SUCCESS" and data.get("vouchers"):
                        return data

                    # Hubble returned non-SUCCESS or empty vouchers
                    last_error = f"Hubble order status: {data.get('status')}, reason: {data.get('failureReason')}"

                except httpx.HTTPStatusError as e:
                    last_error = f"Hubble HTTP {e.response.status_code}: {e.response.text}"
                except httpx.RequestError as e:
                    last_error = f"Hubble request error: {e}"

                # Exponential backoff: 1s, 3s, 9s
                if attempt < 2:
                    await asyncio.sleep(3 ** attempt)

        raise HubbleOrderError(last_error or "Unknown Hubble error")

    async def get_order(self, order_id: str) -> dict | None:
        """Get order details by Hubble order ID."""
        await self._ensure_auth()
        resp = await self._client.get(
            f"/v1/partners/orders/{order_id}",
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


class HubbleOrderError(Exception):
    """Raised when Hubble order fails after retries."""
    pass


# Singleton instance
hubble_service = HubbleService()
