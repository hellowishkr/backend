"""Provider-neutral checkout quote primitives.

Checkout still exposes the existing brand/amount tools to Gemini, but money
movement should depend on a quote-like object rather than scattered provider
helpers. This module is intentionally small: it gives Hubble gift cards the
same shape future local/manual providers will use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class CheckoutQuote:
    provider: str
    provider_product_id: str
    display_name: str
    amount: int
    currency: str = "INR"
    fulfillment_mode: str = "api"
    valid_until: datetime | None = None
    available: bool = True
    user_message: str | None = None
    internal_reason: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def error(self) -> dict | None:
        if self.available:
            return None
        return {
            "error": "provider_quote_unavailable",
            "user_message": self.user_message or "This gift option is not available right now.",
            "internal_reason": self.internal_reason or "provider_quote_unavailable",
            "retryable": False,
        }
