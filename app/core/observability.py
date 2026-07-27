"""Sentry wiring — single place that initialises error tracking.

Keep this module import-safe even when ``sentry-sdk`` isn't installed so
local dev / CI environments that skip it still boot. Production deploys
install ``sentry-sdk[fastapi]==2.20.0`` (see ``requirements.txt``) and
the real SDK is used.

Called exactly once from ``app/main.py`` before any route / DB / HTTP
client is instantiated so Sentry's auto-integrations can patch them.

PII policy
----------
Wishkr processes WhatsApp messages: phone numbers, names, gift content,
personal notes. These are DPDP-classified personal data. We therefore:

  * keep ``send_default_pii=False`` — no request bodies, no cookies;
  * run every event through ``_scrub_pii`` before it leaves the process
    to redact phone numbers in exception messages, breadcrumb payloads,
    and extra-tagged user text.

If you need to debug a specific user turn, use the logs (which are
redacted in production via ``mask_phone``) plus the turn_summary
structured log; Sentry should never be the PII surface.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("wishkr.observability")


# Match Indian mobile numbers embedded in strings. WhatsApp always
# prefixes the country code ``91`` (optionally with ``+``), so we key on
# that to avoid false-positive redactions on unrelated 10-digit numerics
# (order totals, trace ids, etc.). A bare 10-digit number without the
# country code is an acceptable blind spot — our code paths do not leak
# numbers in that format.
#
# Two patterns: (1) compact ``+?91XXXXXXXXXX`` from internal logs, and
# (2) human-formatted variants like ``+91-98765-43210`` / ``91 98765 43210``
# that show up when raw user input or third-party error bodies bubble into
# Sentry. The formatted-form pattern allows spaces / hyphens / parens
# between digit groups but caps total non-digit chars to keep the regex
# linear-time.
_PHONE_RE = re.compile(r"\b\+?91\d{10}\b")
_PHONE_FORMATTED_RE = re.compile(
    r"\+?\s*91[\s\-().]{0,3}\d{2,5}[\s\-().]{0,3}\d{2,5}[\s\-().]{0,3}\d{2,5}"
)
_REDACTED = "91XXXXXXXXXX"


def _scrub_string(value: str) -> str:
    if not isinstance(value, str):
        return value
    # Compact form first (cheap; covers the common case from internal logs),
    # then the looser formatted form to catch user-typed / third-party shapes.
    out = _PHONE_RE.sub(_REDACTED, value)
    return _PHONE_FORMATTED_RE.sub(
        lambda m: _REDACTED if sum(c.isdigit() for c in m.group(0)) >= 12 else m.group(0),
        out,
    )


def _scrub_value(value: Any) -> Any:
    """Recursively redact phone numbers in nested dict/list/string payloads."""
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


def _scrub_event(event: dict, hint: dict) -> dict | None:
    """Sentry ``before_send`` hook — runs on every outbound event.

    Returning ``None`` drops the event entirely (we don't use this path —
    prefer to redact and forward so signal isn't lost). Returning the
    mutated event forwards the redacted copy.
    """
    try:
        for crumb in event.get("breadcrumbs", {}).get("values", []) or []:
            if "message" in crumb:
                crumb["message"] = _scrub_value(crumb.get("message"))
            if "data" in crumb and isinstance(crumb["data"], dict):
                crumb["data"] = _scrub_value(crumb["data"])

        for exc in event.get("exception", {}).get("values", []) or []:
            if "value" in exc:
                exc["value"] = _scrub_value(exc.get("value"))

        if "message" in event:
            event["message"] = _scrub_value(event["message"])

        extra = event.get("extra")
        if isinstance(extra, dict):
            event["extra"] = _scrub_value(extra)

        tags = event.get("tags")
        if isinstance(tags, dict):
            event["tags"] = {k: _scrub_value(v) for k, v in tags.items()}

        # Request body / query / form — scrub even though send_default_pii=False
        # already strips most of it, because FastAPI integration can leak
        # custom tags set via sentry_sdk.set_context.
        request = event.get("request")
        if isinstance(request, dict):
            event["request"] = _scrub_value(request)
    except Exception:
        # Never let scrubbing crash event delivery.
        logger.exception("sentry scrub crashed — forwarding event as-is")
    return event


def init_sentry(settings) -> bool:
    """Initialise Sentry from the app Settings object.

    No-ops (returns False) when ``SENTRY_DSN`` is empty so local / test
    envs don't need an account. Returns True when Sentry was initialised.
    """
    dsn = (getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        logger.info("sentry: SENTRY_DSN empty — error tracking disabled")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
    except Exception:
        logger.exception("sentry: sentry-sdk not importable — skipping init")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=getattr(settings, "app_env", "development"),
        release=(getattr(settings, "git_sha", "") or None),
        traces_sample_rate=float(getattr(settings, "sentry_traces_sample_rate", 0.05)),
        profiles_sample_rate=0.0,
        # Our own scrubber handles PII — tell Sentry not to collect IP,
        # cookies, request bodies by default.
        send_default_pii=False,
        # Drop request bodies entirely. We still get method/path/status
        # via FastAPI's integration without the personal payload.
        max_request_body_size="never",
        # Don't capture full stack locals (they often contain phone / token).
        include_local_variables=False,
        before_send=_scrub_event,  # type: ignore[arg-type]
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            HttpxIntegration(),
            AsyncioIntegration(),
            LoggingIntegration(
                level=logging.INFO,          # capture INFO+ as breadcrumbs
                event_level=logging.ERROR,   # promote ERROR+ to events
            ),
        ],
    )

    # Uvicorn logs full tracebacks to `uvicorn.error` on reload/shutdown
    # (uvloop callback cancellation), which LoggingIntegration otherwise
    # promotes to Sentry events. Real request errors still reach Sentry
    # via the FastAPI/Starlette integrations.
    ignore_logger("uvicorn.error")

    logger.info(
        "sentry: initialised env=%s release=%s traces_sample_rate=%.3f",
        getattr(settings, "app_env", "?"),
        getattr(settings, "git_sha", "") or "<unset>",
        float(getattr(settings, "sentry_traces_sample_rate", 0.05)),
    )
    return True


def capture_exception(exc: BaseException, **extra: Any) -> None:
    """Best-effort capture — no-op if Sentry isn't installed or configured.

    Use at every point where we intentionally swallow an exception to
    keep the app resilient (agent fallback, background jobs, tool
    wrappers). Extra kwargs attach to the Sentry event's ``extra`` for
    grouping / debugging.
    """
    try:
        import sentry_sdk
    except Exception:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for k, v in (extra or {}).items():
                scope.set_extra(k, _scrub_value(v))
            sentry_sdk.capture_exception(exc)
    except Exception:
        # Don't let observability break the app.
        logger.exception("capture_exception failed")
