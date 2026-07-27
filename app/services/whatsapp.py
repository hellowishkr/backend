"""Meta WhatsApp Cloud API client — send messages to users.

Direct Meta API = ~50% cheaper than BSPs (Interakt, Wati).
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from app.core.config import settings
from app.utils.observability import mask_phone

logger = logging.getLogger("wishkr.whatsapp")


# Meta access tokens occasionally rotate / get revoked. Auto-refresh on a
# Cloud API "system user" token is impractical without OAuth flow, so the
# best we can do is detect 401s and alert the founder loudly + once per
# hour (otherwise every send during a token outage spams the alert).
_LAST_META_401_ALERT_TS: float = 0.0
_META_401_ALERT_INTERVAL_SECONDS = 3600

# Meta's customer-service window is 24h from the user's last inbound message.
# Within it, free-text replies are free; outside it, we must use an approved
# template. Use 23h as the cutoff to leave headroom for clock skew + send
# latency (Meta enforces strictly at the moment of delivery, not at our
# branch decision).
_CS_WINDOW = timedelta(hours=23)


def is_in_cs_window(last_inbound_at: datetime | None) -> bool:
    """True iff a free-text WhatsApp send is allowed to this user right now."""
    if last_inbound_at is None:
        return False
    ts = last_inbound_at if last_inbound_at.tzinfo else last_inbound_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) < _CS_WINDOW


# Meta rejects template variables containing newlines, tabs, or 4+ consecutive
# spaces, and caps total payload size. 900 chars leaves headroom under the
# 1024-char body-param ceiling.
_PARAM_MAX_LEN = 900


def sanitize_template_param(value: str) -> str:
    """Flatten a string into a Meta-legal template variable."""
    flat = " ".join(value.split())
    return flat[:_PARAM_MAX_LEN]


async def send_text_or_template(
    phone: str,
    text: str,
    *,
    last_inbound_at: datetime | None,
    template_name: str,
    body_params: list[str],
) -> dict:
    """Send ``text`` free-form if the CS window is open, else the template.

    Outside the 24h customer-service window Meta accepts ``send_text`` with
    HTTP 200 + wamid but silently drops the message (incidents 2026-04-29
    and 2026-06-10). Every business-initiated send that can fire >24h after
    the recipient's last inbound must go through here (or replicate this
    branch). ``body_params`` are sanitized — newlines flattened, length
    capped — because Meta rejects them otherwise.
    """
    if is_in_cs_window(last_inbound_at):
        return await whatsapp_service.send_text(phone, text)
    return await whatsapp_service.send_template(
        phone,
        template_name=template_name,
        body_params=[sanitize_template_param(p) for p in body_params],
    )


async def _alert_meta_401_once(detail: str) -> None:
    global _LAST_META_401_ALERT_TS
    now = time.time()
    if now - _LAST_META_401_ALERT_TS < _META_401_ALERT_INTERVAL_SECONDS:
        return
    _LAST_META_401_ALERT_TS = now
    try:
        from app.utils.alerts import send_founder_alert
        await send_founder_alert(
            "🔴 Meta WhatsApp API returned 401 — token expired or revoked. "
            "Outbound messaging is BROKEN. Rotate WHATSAPP_ACCESS_TOKEN now.\n"
            f"Detail: {detail[:200]}"
        )
    except Exception:
        logger.exception("Failed to alert on Meta 401")

META_API_BASE = "https://graph.facebook.com/v21.0"

# Network knobs.
# 5 s is comfortably above Meta's p99 (~800 ms) but well under our 6 s
# Gemini turn budget, so the send never dominates total latency.
_HTTP_TIMEOUT_SECONDS = 5.0
# Single retry — covers Meta's transient 5xx / read-timeout blips without
# turning into a loop. Anything that needs more than 2 attempts is a real
# outage; the caller's fallback (or the circuit breaker) takes over.
_HTTP_RETRY_BACKOFF_SECONDS = 0.25


class WhatsAppService:
    """Meta WhatsApp Cloud API client."""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)

    async def _post_with_retry(self, payload: dict) -> httpx.Response:
        """POST to Meta with one transient-retry. Raises on the second failure."""
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post(
                    self._url, json=payload, headers=self._headers(),
                )
                resp.raise_for_status()
                return resp
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
            except httpx.HTTPStatusError as e:
                # 401 typically means the access token rotated or was
                # revoked — every outbound send will fail until it's fixed.
                # Surface to the founder once per hour and bail; retrying
                # won't help.
                if e.response.status_code == 401:
                    await _alert_meta_401_once(e.response.text or "401 Unauthorized")
                    raise
                # Only retry server-side glitches; other 4xx is our bug.
                # Log the full Meta error body so we can diagnose template
                # approval failures, bad parameters, etc.
                if e.response.status_code < 500:
                    logger.error(
                        "Meta API %d — response body: %s",
                        e.response.status_code,
                        e.response.text,
                    )
                    raise
                last_exc = e
            if attempt == 0:
                await asyncio.sleep(_HTTP_RETRY_BACKOFF_SECONDS)
        assert last_exc is not None
        raise last_exc

    @property
    def _url(self) -> str:
        return f"{META_API_BASE}/{settings.whatsapp_phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    def _format_phone(self, phone: str) -> str:
        """Ensure phone is in international format without '+' (e.g. 919876543210).

        Rules, in order:
          - Strip non-digit chars.
          - 10 digits → assume IN, prepend ``91``. Registration only elicits
            10-digit numbers from IN users today; non-IN onboarding will
            need an explicit country selector before bare 10-digit non-IN
            arrives here.
          - 11 digits starting with ``0`` → IN with the carrier-prefix
            legacy 0; strip and 91-prefix.
          - 11–15 digits → assumed already country-prefixed (covers
            91XXXXXXXXXX as well as 1XXXXXXXXXX US, 44XXXXXXXXXX UK,
            971XXXXXXXXX UAE, etc.).
          - <10 or >15 digits → raise. Sending a malformed number to Meta
            burns the retry budget on a request Meta will reject every
            time; failing fast surfaces the bad input at the call site so
            the DLQ doesn't quietly fill with poison.
        """
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            return "91" + digits
        if len(digits) == 11 and digits.startswith("0"):
            return "91" + digits[1:]
        if 11 <= len(digits) <= 15:
            return digits
        raise ValueError(
            f"_format_phone: malformed phone (digit_count={len(digits)})"
        )

    async def send_text(self, phone: str, message: str) -> dict:
        """Send a plain text message via WhatsApp."""
        payload = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(phone),
            "type": "text",
            "text": {"body": message},
        }
        resp = await self._post_with_retry(payload)
        data = resp.json()
        logger.info(
            "Sent text to %s: %s",
            mask_phone(phone), data.get("messages", [{}])[0].get("id", ""),
        )
        return data

    async def send_template(
        self,
        phone: str,
        template_name: str,
        body_params: list[str] | None = None,
        header_params: list[str] | None = None,
    ) -> dict:
        """Send a pre-approved WhatsApp template message."""
        components: list[dict[str, Any]] = []

        if header_params:
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": p} for p in header_params],
            })

        if body_params:
            # Sanitize here so every caller is protected — Meta rejects params
            # with newlines/tabs/4+ spaces or over the length ceiling.
            components.append({
                "type": "body",
                "parameters": [
                    {"type": "text", "text": sanitize_template_param(p)} for p in body_params
                ],
            })

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(phone),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "en"},
            },
        }

        if components:
            payload["template"]["components"] = components

        resp = await self._post_with_retry(payload)
        data = resp.json()
        logger.info(
            "Sent template %s to %s: %s",
            template_name,
            mask_phone(phone),
            data.get("messages", [{}])[0].get("id", ""),
        )
        return data

    async def send_buttons(
        self,
        phone: str,
        body: str,
        buttons: list[dict[str, str]],
    ) -> dict:
        """Send an interactive button message. Max 3 buttons."""
        payload = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(phone),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                        for b in buttons[:3]
                    ],
                },
            },
        }
        resp = await self._post_with_retry(payload)
        return resp.json()

    async def send_list(
        self,
        phone: str,
        body: str,
        button_text: str,
        sections: list[dict],
    ) -> dict:
        """Send an interactive list message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": self._format_phone(phone),
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_text,
                    "sections": sections,
                },
            },
        }
        resp = await self._post_with_retry(payload)
        return resp.json()


# Singleton instance
whatsapp_service = WhatsAppService()
