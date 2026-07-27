"""Verify Hubble's create_order idempotency on repeated reference_id.

Run against the **staging** workspace ONLY. Loads two orders with the same
``reference_id`` (~5 seconds apart) and prints what comes back. Three legit
shapes from Hubble:

  1. Same ``id`` + same ``vouchers[0].cardNumber``  → idempotent (✅ safe)
  2. New ``id`` + new ``vouchers[0].cardNumber``    → NOT idempotent (❌ wrap our
     side with idempotency keys before flipping prod)
  3. Second call returns an error (e.g. duplicate-reference)
                                                    → semi-idempotent; treat
                                                      duplicate-reference as a
                                                      no-op in production code

Pre-reqs: HUBBLE_CLIENT_ID / HUBBLE_CLIENT_SECRET / HUBBLE_BASE_URL in env
(staging values). Requires a tiny pre-funded staging wallet — the script
uses ₹10 vouchers to keep cost negligible if the API turns out to
double-bill.

Usage:
  python -m scripts.test_hubble_idempotency [PRODUCT_ID]

If PRODUCT_ID is omitted, the first active brand from the catalogue is used.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from app.core.config import settings
from app.services.hubble import hubble_service


async def _pick_product_id() -> str:
    brands = await hubble_service.get_brands()
    actives = [b for b in brands if b.get("isActive") is not False]
    if not actives:
        raise SystemExit("No active brands in staging catalogue")
    return actives[0]["productId"]


def _summary(label: str, resp: dict) -> dict:
    voucher = (resp.get("vouchers") or [{}])[0]
    return {
        "label": label,
        "order_id": resp.get("id"),
        "status": resp.get("status"),
        "card_number": voucher.get("cardNumber"),
        "card_pin_present": bool(voucher.get("cardPin")),
    }


async def main(product_id: str | None) -> None:
    if "api.dev." not in settings.hubble_base_url and "staging" not in settings.hubble_base_url:
        sys.exit(
            f"REFUSING TO RUN: hubble_base_url={settings.hubble_base_url} "
            f"does not look like a staging URL"
        )

    if product_id is None:
        product_id = await _pick_product_id()

    ref = f"idempotest_{int(time.time())}"
    args = dict(
        product_id=product_id,
        amount=10,
        denomination=10,
        quantity=1,
        reference_id=ref,
        customer_name="Idempotency Test",
        customer_phone="919999999999",
    )

    print(f"reference_id={ref}  product_id={product_id}")
    print("First call …")
    try:
        first = await hubble_service.create_order(**args)
    except Exception as e:
        print(f"First call FAILED: {e}")
        return
    s1 = _summary("first", first)
    print(json.dumps(s1, indent=2))

    print("Sleeping 5s before replay …")
    await asyncio.sleep(5)

    print("Second call (same reference_id) …")
    try:
        second = await hubble_service.create_order(**args)
    except Exception as e:
        print(f"Second call FAILED: {e}")
        print("→ Hubble appears to reject duplicate reference_id (semi-idempotent).")
        print("→ Production code should catch and treat as no-op.")
        return
    s2 = _summary("second", second)
    print(json.dumps(s2, indent=2))

    same_order = s1["order_id"] == s2["order_id"]
    same_card = s1["card_number"] == s2["card_number"]
    print()
    print(f"same_order_id={same_order}  same_card_number={same_card}")
    if same_order and same_card:
        print("✅ Hubble is idempotent on reference_id. Code is safe.")
    else:
        print("❌ Hubble is NOT idempotent on reference_id.")
        print("   Wrap create_order with our own dedup table before going prod.")


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(pid))
