"""Tests for the Sentry wiring in ``app/core/observability.py``.

Goal: lock the contract, never hit the real Sentry network in CI.

  * ``init_sentry`` is a no-op when SENTRY_DSN is empty (local dev).
  * ``capture_exception`` is safe when Sentry isn't initialised.
  * PII scrubber redacts Indian phone numbers in every field Sentry
    could carry them (breadcrumbs, exception value, extra, tags,
    request payload).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def scrub():
    from app.core.observability import _scrub_event
    return _scrub_event


# ─────────────────────────────────────────────────────────
# init_sentry
# ─────────────────────────────────────────────────────────


def test_init_sentry_noop_when_dsn_empty():
    from app.core.observability import init_sentry
    s = SimpleNamespace(sentry_dsn="", app_env="development")
    assert init_sentry(s) is False


def test_init_sentry_noop_when_dsn_whitespace():
    from app.core.observability import init_sentry
    s = SimpleNamespace(sentry_dsn="   \t\n", app_env="development")
    assert init_sentry(s) is False


# ─────────────────────────────────────────────────────────
# capture_exception — safe to call when Sentry uninitialised
# ─────────────────────────────────────────────────────────


def test_capture_exception_is_safe_without_init():
    from app.core.observability import capture_exception
    # Must not raise, must not network. If Sentry isn't initialised,
    # capture is a best-effort no-op.
    try:
        raise RuntimeError("synthetic")
    except RuntimeError as e:
        capture_exception(e, phone="91XXXXXXXXXX")
    # Success = reached here without a second exception.


# ─────────────────────────────────────────────────────────
# PII scrubber — phone-number redaction
# ─────────────────────────────────────────────────────────


def test_scrub_redacts_phone_in_exception_value(scrub):
    event = {
        "exception": {"values": [
            {"type": "ValueError", "value": "User 919876543210 not found"},
        ]},
    }
    out = scrub(event, {})
    assert "919876543210" not in out["exception"]["values"][0]["value"]
    assert "91XXXXXXXXXX" in out["exception"]["values"][0]["value"]


def test_scrub_redacts_phone_in_breadcrumb_message_and_data(scrub):
    event = {
        "breadcrumbs": {"values": [
            {
                "message": "tool_call for 919876543210",
                "data": {"phone": "919876543210", "nested": {"user": "919876543210"}},
            },
        ]},
    }
    out = scrub(event, {})
    crumb = out["breadcrumbs"]["values"][0]
    assert "919876543210" not in crumb["message"]
    assert crumb["data"]["phone"] == "91XXXXXXXXXX"
    assert crumb["data"]["nested"]["user"] == "91XXXXXXXXXX"


def test_scrub_redacts_phone_in_extra_and_message(scrub):
    event = {
        "message": "fired for 917769969505",
        "extra": {"user_msg": "send gift to 919359615424 now", "count": 3},
    }
    out = scrub(event, {})
    assert "917769969505" not in out["message"]
    assert "919359615424" not in out["extra"]["user_msg"]
    assert out["extra"]["count"] == 3  # non-PII untouched


def test_scrub_redacts_phone_with_plus_prefix(scrub):
    """+91 prefix is common in international formatting."""
    event = {"message": "alerted +919876543210 already"}
    out = scrub(event, {})
    assert "919876543210" not in out["message"]


def test_scrub_ignores_10_digit_numbers_that_arent_phones(scrub):
    """A 10-digit number without the 91 prefix shouldn't get touched —
    it could legitimately be an order total in paise, a UUID fragment,
    etc. We accept false negatives over false positives for non-phone
    numerics; the 91-prefix guard is strong enough for our actual
    phone-handling code paths."""
    event = {"message": "order total 1234567890 paise"}
    out = scrub(event, {})
    # Current regex requires 91 prefix, so 1234567890 passes through.
    # This is documented behaviour; change only with deliberate thought.
    assert "1234567890" in out["message"]


def test_scrub_never_raises_on_malformed_event(scrub):
    """Scrubber must never drop / crash an event on unexpected shapes —
    that would bury real errors. Verify with a deliberately weird event."""
    event = {"exception": None, "breadcrumbs": "not-a-dict", "extra": 42}
    # Must not raise.
    out = scrub(event, {})
    assert out is event  # same object, mutated (or left alone)


def test_scrub_preserves_event_identity(scrub):
    """Ensure the scrubber returns the same event dict (mutated in place),
    not None — returning None from ``before_send`` would drop the event
    entirely, which would silently eat Sentry signal."""
    event = {"message": "clean event"}
    assert scrub(event, {}) is event
