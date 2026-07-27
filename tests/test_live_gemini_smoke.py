"""Opt-in live Gemini smoke test for the WhatsApp bot.

This is intentionally NOT part of normal CI:

    RUN_LIVE_GEMINI_TESTS=1 .venv/bin/python -m pytest tests/test_live_gemini_smoke.py -q -s

What is real:
- the FastAPI WhatsApp webhook route
- the bot's real ``handle_message`` path
- Gemini / Google ADK calls
- the configured database/session store

What is faked:
- Meta webhook signature validation
- outbound WhatsApp send_text, so no real user is messaged

Use a dedicated dev database and a throwaway phone number. This test writes
normal conversation/session/idempotency rows, just like a real webhook would.
"""

from __future__ import annotations

import os
import time

import pytest


if os.getenv("RUN_LIVE_GEMINI_TESTS") != "1":
    pytest.skip(
        "live Gemini smoke skipped; set RUN_LIVE_GEMINI_TESTS=1 to run",
        allow_module_level=True,
    )


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.core.config import settings  # noqa: E402

if settings.gemini_api_key:
    os.environ.setdefault("GOOGLE_API_KEY", settings.gemini_api_key)

try:
    from app.webhooks import whatsapp as whatsapp_webhook  # noqa: E402
except OperationalError as exc:
    pytest.skip(
        f"live Gemini smoke skipped; configured database is unavailable: {exc}",
        allow_module_level=True,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(whatsapp_webhook.router, prefix="/webhooks")
    return TestClient(app)


def _meta_text_payload(*, wamid: str, phone: str, text: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_live_smoke",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "123456789",
                            },
                            "messages": [
                                {
                                    "from": phone,
                                    "id": wamid,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_live_gemini_whatsapp_webhook_smoke(monkeypatch):
    """Send one short inbound message through the real webhook + Gemini path."""
    if not settings.gemini_api_key:
        pytest.skip("GEMINI_API_KEY is not configured")

    outbound: list[tuple[str, str]] = []

    async def fake_send_text(phone: str, message: str):
        outbound.append((phone, message))
        return {"messages": [{"id": "wamid.live-smoke.outbound"}]}

    monkeypatch.setattr(whatsapp_webhook, "_has_valid_meta_signature", lambda *_args: True)
    monkeypatch.setattr(whatsapp_webhook.whatsapp_service, "send_text", fake_send_text)
    # Keep the smoke deterministic: "hi" takes the immediate path and should
    # wait for the final bot reply instead of spawning a background worker.
    monkeypatch.setattr(whatsapp_webhook, "should_send_ack", lambda _text: False)

    phone = os.getenv("LIVE_GEMINI_SMOKE_PHONE", "919000000001")
    wamid = f"wamid.live-smoke.{int(time.time() * 1000)}"

    response = _client().post(
        "/webhooks/whatsapp",
        json=_meta_text_payload(
            wamid=wamid,
            phone=phone,
            text=os.getenv("LIVE_GEMINI_SMOKE_TEXT", "hi"),
        ),
        timeout=30,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert outbound, "bot did not attempt to send an outbound WhatsApp reply"

    sent_to, body = outbound[-1]
    assert sent_to == phone
    assert body.strip()
    assert "trouble reaching the AI" not in body
    assert "got a bit stuck" not in body.lower()
