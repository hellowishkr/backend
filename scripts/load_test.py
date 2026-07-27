#!/usr/bin/env python3
"""
WhatsApp webhook load test for Wishkr staging only.

NEVER run this against production.

Usage:
  python wishkr-backend/scripts/load_test.py \
    --url https://staging-api.example.com/webhooks/whatsapp \
    --webhook-secret "$WHATSAPP_WEBHOOK_SECRET" \
    --start-users 1 \
    --end-users 100 \
    --ramp-seconds 60 \
    --hold-seconds 300

What it does:
  - Simulates Meta-style webhook payloads (text, button reply, list reply, contacts share).
  - Ramps concurrent simulated users from start-users to end-users over ramp-seconds.
  - Holds steady for hold-seconds.
  - Reports latency P50/P95/P99, error rate, and throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx


DEFAULT_PATH = "/webhooks/whatsapp"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_THINK_MIN = 1.5
DEFAULT_THINK_MAX = 4.0


SAFE_HOST_MARKERS = (
    "localhost",
    "127.0.0.1",
    "staging",
    "dev",
    "test",
    "preview",
    "ngrok",
)

PRODUCTION_HOST_BLOCKLIST = (
    "wishkr.com",
    "api.wishkr.com",
)


TEXT_MESSAGES = [
    "Hi",
    "Add my sister birthday on 14 Feb",
    "Suggest gifts for budget 1000 to 2000",
    "show my occasions",
    "remind me later",
]

BUTTON_TITLES = [
    ("send_gift", "Send a Gift"),
    ("remind_later", "Remind Me Later"),
    ("yes_setup", "Yes, let's do it"),
]

LIST_TITLES = [
    ("menu_occasions", "My Occasions"),
    ("menu_wishlist", "Wishlist"),
    ("menu_help", "Help"),
]


def _normalize_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if not parsed.scheme:
        raw_url = "http://" + raw_url
        parsed = urlparse(raw_url)

    if not parsed.path or parsed.path == "/":
        return raw_url.rstrip("/") + DEFAULT_PATH
    return raw_url


def _is_blocked_production_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    host = hostname.lower()
    if any(marker in host for marker in SAFE_HOST_MARKERS):
        return False
    if any(bad == host or host.endswith("." + bad) for bad in PRODUCTION_HOST_BLOCKLIST):
        return True
    return True


def _meta_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def _phone_for_user(user_idx: int) -> str:
    base = 7000000000 + (user_idx % 1_000_000_000)
    return "91" + str(base).zfill(10)


def _make_message(user_idx: int) -> dict[str, Any]:
    msg_id = "wamid." + uuid.uuid4().hex
    from_phone = _phone_for_user(user_idx)
    msg_type = random.choice(["text", "interactive_button", "interactive_list", "contacts"])

    if msg_type == "text":
        return {
            "id": msg_id,
            "from": from_phone,
            "timestamp": str(int(time.time())),
            "type": "text",
            "text": {"body": random.choice(TEXT_MESSAGES)},
        }

    if msg_type == "interactive_button":
        rid, title = random.choice(BUTTON_TITLES)
        return {
            "id": msg_id,
            "from": from_phone,
            "timestamp": str(int(time.time())),
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": rid, "title": title},
            },
        }

    if msg_type == "interactive_list":
        rid, title = random.choice(LIST_TITLES)
        return {
            "id": msg_id,
            "from": from_phone,
            "timestamp": str(int(time.time())),
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": rid, "title": title},
            },
        }

    contact_phone = _phone_for_user(user_idx + 100_000)
    return {
        "id": msg_id,
        "from": from_phone,
        "timestamp": str(int(time.time())),
        "type": "contacts",
        "contacts": [
            {
                "name": {"formatted_name": f"Friend {user_idx}"},
                "phones": [{"wa_id": contact_phone}],
            }
        ],
    }


def _build_payload(user_idx: int) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919999999999",
                                "phone_number_id": "1234567890",
                            },
                            "contacts": [{"wa_id": _phone_for_user(user_idx)}],
                            "messages": [_make_message(user_idx)],
                        },
                    }
                ],
            }
        ],
    }


@dataclass
class Stats:
    started_at: float = field(default_factory=time.time)
    latencies_ms: list[float] = field(default_factory=list)
    total: int = 0
    success: int = 0
    failures: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    network_errors: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, latency_ms: float, status_code: int | None, ok: bool) -> None:
        async with self.lock:
            self.total += 1
            self.latencies_ms.append(latency_ms)
            if ok:
                self.success += 1
            else:
                self.failures += 1
            if status_code is None:
                self.network_errors += 1
            else:
                self.status_counts[status_code] = self.status_counts.get(status_code, 0) + 1


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    idx = int(round((p / 100.0) * (len(values) - 1)))
    idx = max(0, min(idx, len(values) - 1))
    return sorted(values)[idx]


async def _one_request(
    client: httpx.AsyncClient,
    url: str,
    user_idx: int,
    webhook_secret: str | None,
    stats: Stats,
) -> None:
    payload = _build_payload(user_idx)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["X-Hub-Signature-256"] = _meta_signature(webhook_secret, raw)

    t0 = time.perf_counter()
    status_code: int | None = None
    ok = False
    try:
        resp = await client.post(url, content=raw, headers=headers)
        status_code = resp.status_code
        ok = 200 <= resp.status_code < 300
    except httpx.HTTPError:
        ok = False
    finally:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        await stats.record(latency_ms, status_code, ok)


async def _virtual_user(
    user_idx: int,
    client: httpx.AsyncClient,
    url: str,
    webhook_secret: str | None,
    stats: Stats,
    stop_at: float,
    think_min: float,
    think_max: float,
) -> None:
    while time.time() < stop_at:
        await _one_request(client, url, user_idx, webhook_secret, stats)
        await asyncio.sleep(random.uniform(think_min, think_max))


async def run_test(args: argparse.Namespace) -> int:
    url = _normalize_url(args.url)
    parsed = urlparse(url)

    if not args.allow_production and _is_blocked_production_host(parsed.hostname):
        print("ABORTED: target looks non-staging/unsafe. Use --allow-production to override (not recommended).")
        print(f"Target URL: {url}")
        return 2

    random.seed(args.seed)
    stats = Stats()

    total_duration = args.ramp_seconds + args.hold_seconds
    stop_at = time.time() + total_duration

    limits = httpx.Limits(max_connections=500, max_keepalive_connections=200)
    timeout = httpx.Timeout(args.timeout_seconds)

    print("Starting load test (STAGING ONLY)")
    print(f"URL: {url}")
    print(
        f"Ramp: {args.start_users} -> {args.end_users} users in {args.ramp_seconds}s, "
        f"hold {args.hold_seconds}s"
    )
    print(f"Think time/user: {args.think_min:.1f}s to {args.think_max:.1f}s")
    print(f"Signature: {'enabled' if args.webhook_secret else 'disabled'}")

    tasks: list[asyncio.Task] = []
    users_to_add = max(0, args.end_users - args.start_users)
    ramp_interval = (args.ramp_seconds / users_to_add) if users_to_add else 0.0

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        for i in range(args.start_users):
            tasks.append(
                asyncio.create_task(
                    _virtual_user(
                        user_idx=i,
                        client=client,
                        url=url,
                        webhook_secret=args.webhook_secret,
                        stats=stats,
                        stop_at=stop_at,
                        think_min=args.think_min,
                        think_max=args.think_max,
                    )
                )
            )

        for i in range(users_to_add):
            await asyncio.sleep(ramp_interval)
            user_idx = args.start_users + i
            tasks.append(
                asyncio.create_task(
                    _virtual_user(
                        user_idx=user_idx,
                        client=client,
                        url=url,
                        webhook_secret=args.webhook_secret,
                        stats=stats,
                        stop_at=stop_at,
                        think_min=args.think_min,
                        think_max=args.think_max,
                    )
                )
            )

        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = max(0.001, time.time() - stats.started_at)
    throughput = stats.total / elapsed
    error_rate = (stats.failures / stats.total * 100.0) if stats.total else 0.0

    p50 = _percentile(stats.latencies_ms, 50)
    p95 = _percentile(stats.latencies_ms, 95)
    p99 = _percentile(stats.latencies_ms, 99)
    avg = statistics.mean(stats.latencies_ms) if stats.latencies_ms else 0.0

    print("\n=== Load Test Summary ===")
    print(f"Requests: {stats.total}")
    print(f"Success:  {stats.success}")
    print(f"Failures: {stats.failures} ({error_rate:.2f}%)")
    print(f"Throughput: {throughput:.2f} req/s")
    print(f"Latency ms: p50={p50:.1f} p95={p95:.1f} p99={p99:.1f} avg={avg:.1f}")
    print(f"Status codes: {dict(sorted(stats.status_counts.items()))}")
    if stats.network_errors:
        print(f"Network errors: {stats.network_errors}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wishkr WhatsApp webhook load test (staging only).")
    parser.add_argument("--url", required=True, help="Webhook URL (full endpoint or host).")
    parser.add_argument(
        "--webhook-secret",
        default=None,
        help="Meta webhook secret for X-Hub-Signature-256. Required if signature validation is enabled.",
    )
    parser.add_argument("--start-users", type=int, default=1)
    parser.add_argument("--end-users", type=int, default=100)
    parser.add_argument("--ramp-seconds", type=int, default=60)
    parser.add_argument("--hold-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--think-min", type=float, default=DEFAULT_THINK_MIN)
    parser.add_argument("--think-max", type=float, default=DEFAULT_THINK_MAX)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Override safety block. Not recommended.",
    )
    args = parser.parse_args()

    if args.start_users < 1 or args.end_users < args.start_users:
        raise SystemExit("Invalid users range.")
    if args.ramp_seconds < 0 or args.hold_seconds <= 0:
        raise SystemExit("Invalid duration values.")
    if args.think_min < 0 or args.think_max < args.think_min:
        raise SystemExit("Invalid think time range.")
    return args


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
