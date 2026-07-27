import asyncio
import hashlib
import hmac
from typing import Any

import razorpay

from app.core.config import settings


# Concurrency cap on outbound Razorpay calls. The python client is sync and
# runs in the default thread pool — without a cap, N simultaneous tool
# turns can each spin up a thread, exhaust the pool, and starve unrelated
# DB work. 10 is comfortably below any Razorpay published rate limit
# (their default tier is 300 r/m for payment_link create) while keeping
# our threads bounded.
_RAZORPAY_CONCURRENCY = 10
_razorpay_semaphore = asyncio.Semaphore(_RAZORPAY_CONCURRENCY)


class RazorpayService:
    """Razorpay payment link creation and webhook verification."""

    def __init__(self):
        self._client: razorpay.Client | None = None

    @property
    def client(self) -> razorpay.Client:
        if not self._client:
            self._client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        return self._client

    async def create_payment_link(
        self,
        amount: int,  # in paise (₹500 = 50000)
        description: str,
        reference_id: str,
        customer_name: str,
        customer_phone: str,
        customer_email: str = "",
        expire_by: int | None = None,  # Unix timestamp
    ) -> dict[str, Any]:
        """
        Create a Razorpay payment link.

        Returns dict with 'id' (link ID), 'short_url' (payment URL), etc.
        """
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": "INR",
            "description": description,
            "reference_id": reference_id,
            # order_id propagated into downstream webhook events so failures
            # can be mapped back to the correct Order even when the event
            # payload doesn't include the payment_link reference_id.
            "notes": {"order_id": reference_id},
            "customer": {
                "name": customer_name,
                "contact": f"+{customer_phone}" if not customer_phone.startswith("+") else customer_phone,
            },
            "notify": {"sms": False, "email": False},  # We send via WhatsApp ourselves
            "callback_url": "",
            "callback_method": "",
        }

        if customer_email:
            payload["customer"]["email"] = customer_email

        if expire_by:
            payload["expire_by"] = expire_by

        # razorpay-python is sync — run in thread pool, bounded by the
        # service-level semaphore.
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(None, self.client.payment_link.create, payload)

    async def create_order(
        self,
        amount: int,
        receipt: str,
        notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay Order for Checkout JS."""
        payload: dict[str, Any] = {
            "amount": amount,
            "currency": "INR",
            "receipt": receipt,
            "payment_capture": 1,
            "notes": notes or {},
        }
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(None, self.client.order.create, payload)

    async def cancel_payment_link(self, link_id: str) -> dict[str, Any]:
        """Cancel an outstanding payment link on Razorpay.

        Used when we're refreshing a stale link tied to an existing order so
        the original link can't be paid in parallel with the new one (which
        would double-charge the user and double-fulfill the gift).
        """
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(None, self.client.payment_link.cancel, link_id)

    async def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        """Fetch the current state of a payment link from Razorpay.

        Used after a cancel failure to disambiguate "already paid" vs
        "transient API error" — paying a freshly-minted second link while
        the first link is still in a paid state would double-charge the user.
        """
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(None, self.client.payment_link.fetch, link_id)

    async def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch current payment state from Razorpay."""
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(None, self.client.payment.fetch, payment_id)

    async def capture_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Capture an authorized payment. No-ops if already captured (caller ignores the error)."""
        loop = asyncio.get_event_loop()
        async with _razorpay_semaphore:
            return await loop.run_in_executor(
                None,
                lambda: self.client.payment.capture(payment_id, {"amount": amount_paise, "currency": "INR"}),
            )

    async def refund_payment(self, payment_id: str, amount_paise: int | None = None) -> dict[str, Any]:
        """Issue a refund against a captured payment.

        Called from the auto-refund path after Hubble fulfilment fails so
        the user gets their money back without waiting for manual founder
        intervention. ``amount_paise`` is required by the Razorpay SDK —
        omitting it causes BadRequestError.
        """
        loop = asyncio.get_event_loop()
        payload: dict[str, Any] = {}
        if amount_paise is not None:
            payload["amount"] = int(amount_paise)
        async with _razorpay_semaphore:
            return await loop.run_in_executor(
                None,
                lambda: self.client.payment.refund(payment_id, payload),
            )

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """Verify Razorpay webhook HMAC signature."""
        expected = hmac.new(
            settings.razorpay_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_checkout_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify the signature Razorpay Checkout returns to the browser on success.

        Distinct from the webhook signature: this one is HMAC-SHA256 of
        ``"{order_id}|{payment_id}"`` keyed by the account secret, per
        Razorpay's Checkout (order_id flow) documentation.
        """
        expected = hmac.new(
            settings.razorpay_key_secret.encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


# Singleton instance
razorpay_service = RazorpayService()
