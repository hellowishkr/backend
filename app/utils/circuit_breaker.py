"""Tiny in-process circuit breaker for the Gemini call.

Single replica, single-process — no Redis, no shared state. Goal is to stop
hammering Gemini when it's clearly down so we shed load fast and the user
sees a canned fallback instead of waiting for repeated 6-second timeouts.

States:
  CLOSED  — calls go through (default).
  OPEN    — fail fast for `cooldown_seconds` then transition to HALF_OPEN.
  HALF_OPEN — allow ONE probe call; on success → CLOSED, on failure → OPEN.

Tracks failures over a rolling `window_seconds`. Trips when failures within
the window reach `failure_threshold`.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Literal

State = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_seconds: float = 30.0,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds

        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._state: State = "closed"
        self._probe_in_progress = False
        self._lock = Lock()

    @property
    def state(self) -> State:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def allow(self) -> bool:
        """Return True if the call should be attempted."""
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == "closed":
                return True
            if self._state != "half_open":
                return False
            if self._probe_in_progress:
                return False
            self._probe_in_progress = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at = None
            self._state = "closed"
            self._probe_in_progress = False

    def record_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._state == "half_open":
                # The probe failed — re-open.
                self._opened_at = now
                self._state = "open"
                self._probe_in_progress = False
                return

            self._failures.append(now)
            cutoff = now - self.window_seconds
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            if len(self._failures) >= self.failure_threshold:
                self._opened_at = now
                self._state = "open"
                self._probe_in_progress = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "failures_in_window": len(self._failures),
                "opened_at": self._opened_at,
                "probe_in_progress": self._probe_in_progress,
            }

    # -- internal -----------------------------------------------------------
    def _maybe_transition_to_half_open(self) -> None:
        if self._state != "open" or self._opened_at is None:
            return
        if (time.monotonic() - self._opened_at) >= self.cooldown_seconds:
            self._state = "half_open"
            self._probe_in_progress = False
