"""Tiny observability primitives — mask, latency buckets, in-memory metrics.

Single-process counters. Adequate for a single-replica deployment; in
multi-replica land swap `metrics` for a Prometheus client (the call sites
stay the same).
"""

from __future__ import annotations

from threading import Lock


def mask_phone(phone: str | None) -> str:
    """Return a PII-safe representation of a phone number.

    Keeps the last 4 digits so the value is still useful for correlating
    logs without exposing the full identity.
    """
    if not phone:
        return "***"
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) <= 4:
        return "***" + digits
    return "***" + digits[-4:]


def latency_bucket(duration_ms: float) -> str:
    """Coarse bucket for histogram-style log aggregation."""
    if duration_ms < 200:
        return "fast"
    if duration_ms < 1000:
        return "ok"
    if duration_ms < 3000:
        return "slow"
    return "very_slow"


class Metrics:
    """Lock-protected counter bag. `inc` is the only mutator we need."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = Lock()

    def inc(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + by

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()


metrics = Metrics()
