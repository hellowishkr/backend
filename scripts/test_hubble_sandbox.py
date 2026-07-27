"""
Hubble sandbox smoke test.

Exercises the full gift card flow against api.dev.myhubble.money:
  1. Authenticate (clientId/clientSecret → bearer token)
  2. List brands (catalogue)
  3. Create a test order (small denomination on a cheap brand)
  4. Retrieve the order by ID

Run from wishkr-backend/:
  python -m scripts.test_hubble_sandbox
  python -m scripts.test_hubble_sandbox --skip-order   # catalogue only (if account not yet verified)
  python -m scripts.test_hubble_sandbox --product-id <id> --denomination 100
"""
import argparse
import asyncio
import sys
import time
import uuid

from app.core.config import settings
from app.services.hubble import HubbleOrderError, hubble_service


def _h1(msg: str) -> None:
    print(f"\n{'=' * 60}\n{msg}\n{'=' * 60}")


def _pass(msg: str) -> None:
    print(f"  PASS  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}")


def _info(msg: str) -> None:
    print(f"  ...   {msg}")


async def step_auth() -> bool:
    _h1("Step 1: Authenticate")
    _info(f"base_url = {settings.hubble_base_url}")
    _info(f"client_id = {settings.hubble_client_id[:6]}...{settings.hubble_client_id[-4:] if len(settings.hubble_client_id) > 10 else ''}")
    try:
        token = await hubble_service.authenticate()
        ttl = int(hubble_service._token_expires_at - time.time())
        _pass(f"got token (len={len(token)}, ttl={ttl}s)")
        return True
    except Exception as e:
        _fail(f"auth failed: {type(e).__name__}: {e}")
        return False


async def step_catalogue() -> list[dict]:
    _h1("Step 2: Fetch catalogue")
    try:
        brands = await hubble_service.get_brands(force_refresh=True)
        _pass(f"fetched {len(brands)} active brands")
        if brands:
            flex = sum(1 for b in brands if b.get("denominationType") == "FLEXIBLE")
            fixed = sum(1 for b in brands if b.get("denominationType") == "FIXED")
            print(f"\n  breakdown: {flex} FLEXIBLE, {fixed} FIXED")
            print("\n  Sample popular brands:")
            for want in ["Amazon", "Flipkart", "Myntra", "Zomato", "IKEA"]:
                match = next((b for b in brands if want.lower() in (b.get("title") or "").lower()), None)
                if match:
                    ar = match.get("amountRestrictions") or {}
                    print(f"    - {match.get('title')} (id={match['id']}) "
                          f"type={match.get('denominationType')} "
                          f"min={ar.get('minVoucherAmount')} max={ar.get('maxVoucherAmount')}")
        return brands
    except Exception as e:
        _fail(f"catalogue fetch failed: {type(e).__name__}: {e}")
        return []


def pick_test_product(brands: list[dict]) -> tuple[str, int, str] | None:
    """Pick a FIXED brand with the lowest denomination for a deterministic test."""
    candidates: list[tuple[int, str, str]] = []
    for b in brands:
        if b.get("denominationType") != "FIXED":
            continue
        ar = b.get("amountRestrictions") or {}
        denoms = ar.get("denominations") or []
        if denoms:
            min_denom = min(int(d) for d in denoms)
            candidates.append((min_denom, b["id"], b.get("title", "?")))
    if not candidates:
        # Fall back to FLEXIBLE with minVoucherAmount > 0
        for b in brands:
            if b.get("denominationType") == "FLEXIBLE":
                ar = b.get("amountRestrictions") or {}
                mn = ar.get("minVoucherAmount") or 0
                if mn > 0:
                    candidates.append((int(mn), b["id"], b.get("title", "?")))
    if not candidates:
        return None
    candidates.sort()
    denom, pid, title = candidates[0]
    print(f"  picked: {title} (id={pid}) denomination={denom}")
    return pid, denom, title


async def step_order(product_id: str, denomination: int) -> str | None:
    _h1("Step 3: Create test order")
    ref = f"WISHKR-TEST-{uuid.uuid4().hex[:12]}"
    _info(f"productId={product_id} denom={denomination} ref={ref}")
    try:
        order = await hubble_service.create_order(
            product_id=product_id,
            amount=denomination,
            denomination=denomination,
            quantity=1,
            reference_id=ref,
            customer_name="Wishkr Sandbox Test",
            customer_phone="9999999999",
            customer_email="sandbox@wishkr.com",
        )
        order_id = order.get("id") or order.get("orderId")
        vouchers = order.get("vouchers", [])
        _pass(f"order created: id={order_id} status={order.get('status')} vouchers={len(vouchers)}")
        if vouchers:
            v = vouchers[0]
            code = v.get("cardNumber") or ""
            pin = v.get("cardPin") or ""
            masked_code = f"...{code[-4:]}" if code else "(none)"
            masked_pin = f"...{pin[-2:]}" if pin else "(none)"
            print(f"    card: {masked_code}  pin: {masked_pin}  valid till: {v.get('validTill', '?')}  amount: {v.get('amount')}")
        return order_id
    except HubbleOrderError as e:
        _fail(f"order error after retries: {e}")
        return None
    except Exception as e:
        _fail(f"unexpected: {type(e).__name__}: {e}")
        return None


async def step_fetch_order(order_id: str) -> bool:
    _h1("Step 4: Fetch order by ID")
    try:
        order = await hubble_service.get_order(order_id)
        if order is None:
            _fail(f"GET /orders/{order_id} returned 404")
            return False
        _pass(f"fetched order: status={order.get('status')} vouchers={len(order.get('vouchers', []))}")
        return True
    except Exception as e:
        _fail(f"fetch failed: {type(e).__name__}: {e}")
        return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-order", action="store_true", help="Skip order creation (for unverified accounts)")
    ap.add_argument("--product-id", help="Override: test a specific productId")
    ap.add_argument("--denomination", type=int, help="Override: test a specific denomination")
    args = ap.parse_args()

    if "dev" not in settings.hubble_base_url and "staging" not in settings.hubble_base_url:
        print(f"REFUSING: HUBBLE_BASE_URL does not look like sandbox: {settings.hubble_base_url}")
        print("Set HUBBLE_BASE_URL=https://api.dev.myhubble.money before running this test.")
        return 2

    if not await step_auth():
        return 1

    brands = await step_catalogue()
    if not brands:
        print("\nNo brands returned — cannot continue to order step.")
        return 1

    if args.skip_order:
        print("\n--skip-order set: stopping after catalogue.")
        return 0

    if args.product_id and args.denomination:
        product_id, denomination = args.product_id, args.denomination
        print(f"  using override: id={product_id} denom={denomination}")
    else:
        pick = pick_test_product(brands)
        if not pick:
            print("\nCould not auto-pick a brand. Use --product-id/--denomination.")
            return 1
        product_id, denomination, _ = pick

    order_id = await step_order(product_id, denomination)
    if not order_id:
        return 1

    await step_fetch_order(order_id)

    _h1("Summary")
    print("  Auth, catalogue, order, fetch — all steps exercised.")
    print(f"  NOTE: This was sandbox ({settings.hubble_base_url}). No real money moved.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
