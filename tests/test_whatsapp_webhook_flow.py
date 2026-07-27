"""Automation tests for the WhatsApp bot webhook flow.

These tests exercise the same HTTP surface Meta calls, but replace DB/network
edges with in-memory fakes so the flow is safe to run locally and in CI.
"""

from __future__ import annotations

import google.adk.sessions as _adk_sessions
import logging
import time
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.exc import OperationalError


class _StubSessionService:  # noqa: D401 - test stub
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]


from app.webhooks import whatsapp as whatsapp_webhook  # noqa: E402
from app.bot import agent  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(whatsapp_webhook.router, prefix="/webhooks")
    return TestClient(app)


def _meta_text_payload(*, wamid: str = "wamid.test.1", phone: str = "9876543210", text: str = "hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_test",
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
                                    "timestamp": "1710000000",
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


def _meta_status_payload(
    *,
    wamid: str = "wamid.test.status",
    phone: str = "919876545424",
    status: str = "failed",
):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_test",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15551234567",
                                "phone_number_id": "123456789",
                            },
                            "statuses": [
                                {
                                    "id": wamid,
                                    "recipient_id": phone,
                                    "status": status,
                                    "timestamp": "1710000000",
                                    "errors": [
                                        {
                                            "code": 131026,
                                            "title": "Message undeliverable",
                                            "message": "Message undeliverable",
                                            "error_data": {"details": "Test failure details"},
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_whatsapp_webhook_routes_immediate_text_to_bot(monkeypatch):
    seen: dict[str, str] = {}
    sent: list[tuple[str, str]] = []

    async def claim(_wamid: str) -> bool:
        return True

    async def noop(*_args, **_kwargs):
        return None

    async def no_fast_path(_phone: str, _message: str):
        return None

    async def fake_process(phone: str, text: str) -> str:
        seen["phone"] = phone
        seen["text"] = text
        return "Bot reply from test"

    async def fake_send(phone: str, response: str) -> None:
        sent.append((phone, response))

    monkeypatch.setattr(whatsapp_webhook, "_claim_processed_message", claim)
    monkeypatch.setattr(whatsapp_webhook, "_has_valid_meta_signature", lambda *_args: True)
    monkeypatch.setattr(whatsapp_webhook, "_stamp_last_inbound", noop)
    monkeypatch.setattr(whatsapp_webhook, "_run_fast_router", no_fast_path)
    monkeypatch.setattr(whatsapp_webhook, "_mark_processed_done", noop)
    monkeypatch.setattr(whatsapp_webhook, "_process_message", fake_process)
    monkeypatch.setattr(whatsapp_webhook, "_send_reply", fake_send)
    monkeypatch.setattr(whatsapp_webhook.message_rate_limiter, "is_allowed", lambda _phone: True)
    monkeypatch.setattr(whatsapp_webhook._burst_rate_limiter, "is_allowed", lambda _phone: True)
    monkeypatch.setattr(whatsapp_webhook, "should_send_ack", lambda _text: False)

    response = _client().post(
        "/webhooks/whatsapp",
        json=_meta_text_payload(text="hi"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert seen == {"phone": "919876543210", "text": "hi"}
    assert sent == [("919876543210", "Bot reply from test")]


def test_whatsapp_webhook_logs_status_callbacks(monkeypatch, caplog):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("status webhook should not enter inbound message flow")

    monkeypatch.setattr(whatsapp_webhook, "_has_valid_meta_signature", lambda *_args: True)
    monkeypatch.setattr(whatsapp_webhook, "_claim_processed_message", fail_if_called)

    caplog.set_level(logging.INFO, logger="wishkr.webhooks.whatsapp")

    response = _client().post(
        "/webhooks/whatsapp",
        json=_meta_status_payload(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "WhatsApp status failed phone=***5424" in caplog.text
    assert "code=131026" in caplog.text


def test_whatsapp_webhook_skips_duplicate_wamid(monkeypatch):
    called = False

    async def duplicate(_wamid: str) -> bool:
        return False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("duplicate message should not reach bot flow")

    monkeypatch.setattr(whatsapp_webhook, "_claim_processed_message", duplicate)
    monkeypatch.setattr(whatsapp_webhook, "_has_valid_meta_signature", lambda *_args: True)
    monkeypatch.setattr(whatsapp_webhook, "_process_message", fail_if_called)
    monkeypatch.setattr(whatsapp_webhook, "_send_reply", fail_if_called)

    response = _client().post(
        "/webhooks/whatsapp",
        json=_meta_text_payload(wamid="wamid.duplicate", text="hi"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert called is False


def test_whatsapp_webhook_slow_path_sends_ack_and_spawns_worker(monkeypatch):
    acks: list[tuple[str, str]] = []
    spawned: list[str] = []

    async def claim(_wamid: str) -> bool:
        return True

    async def noop(*_args, **_kwargs):
        return None

    async def no_fast_path(_phone: str, _message: str):
        return None

    async def fake_ack(phone: str, text: str) -> None:
        acks.append((phone, text))

    def fake_spawn(coro):
        spawned.append("worker")
        coro.close()
        return None

    monkeypatch.setattr(whatsapp_webhook, "_claim_processed_message", claim)
    monkeypatch.setattr(whatsapp_webhook, "_has_valid_meta_signature", lambda *_args: True)
    monkeypatch.setattr(whatsapp_webhook, "_stamp_last_inbound", noop)
    monkeypatch.setattr(whatsapp_webhook, "_run_fast_router", no_fast_path)
    monkeypatch.setattr(whatsapp_webhook, "_send_ack", fake_ack)
    monkeypatch.setattr(whatsapp_webhook, "_spawn_tracked", fake_spawn)
    monkeypatch.setattr(whatsapp_webhook.message_rate_limiter, "is_allowed", lambda _phone: True)
    monkeypatch.setattr(whatsapp_webhook._burst_rate_limiter, "is_allowed", lambda _phone: True)
    monkeypatch.setattr(whatsapp_webhook, "should_send_ack", lambda _text: True)

    response = _client().post(
        "/webhooks/whatsapp",
        json=_meta_text_payload(text="please suggest a gift card for my wife"),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert acks == [("919876543210", "please suggest a gift card for my wife")]
    assert spawned == ["worker"]


@pytest.mark.asyncio
async def test_immediate_exception_marks_processed_failed(monkeypatch):
    sent: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    done: list[str] = []

    async def boom(_phone: str, _text: str) -> str:
        raise RuntimeError("handler exploded")

    async def fake_fallback(phone: str) -> None:
        sent.append((phone, "fallback"))

    async def fake_failed(wamid: str, phase: str, error_type: str) -> None:
        failed.append((wamid, phase, error_type))

    async def fake_done(wamid: str) -> None:
        done.append(wamid)

    monkeypatch.setattr(whatsapp_webhook, "_process_message", boom)
    monkeypatch.setattr(whatsapp_webhook, "_emit_fallback", fake_fallback)
    monkeypatch.setattr(whatsapp_webhook, "_mark_processed_failed", fake_failed)
    monkeypatch.setattr(whatsapp_webhook, "_mark_processed_done", fake_done)

    await whatsapp_webhook._process_immediate_and_reply(
        "919876543210", "hi", wamid="wamid.failed"
    )

    assert sent == [("919876543210", "fallback")]
    assert failed == [("wamid.failed", "immediate", "RuntimeError")]
    assert done == []


class _TransientThenOkSessionService:
    def __init__(self):
        self.list_calls = 0
        self.create_calls = 0
        self.db_engine = SimpleNamespace(dispose=lambda: None)

    async def list_sessions(self, *, app_name: str, user_id: str):
        self.list_calls += 1
        if self.list_calls == 1:
            raise OperationalError(
                statement="SELECT 1",
                params={},
                orig=Exception("server closed the connection unexpectedly"),
            )
        return SimpleNamespace(sessions=[])

    async def create_session(self, *, app_name: str, user_id: str):
        self.create_calls += 1
        return SimpleNamespace(
            app_name=app_name,
            user_id=user_id,
            id="sess-ok",
            events=[],
            last_update_time=time.time(),
        )


@pytest.mark.asyncio
async def test_adk_session_retry_path_sends_one_reply(monkeypatch):
    first = _TransientThenOkSessionService()
    second = first
    sent: list[tuple[str, str]] = []
    done: list[str] = []
    failed: list[tuple[str, str, str]] = []

    monkeypatch.setattr(agent, "_session_service", first, raising=False)
    monkeypatch.setattr(agent, "_build_session_service", lambda: second)
    agent._runners.clear()

    async def fake_process(phone: str, _text: str) -> str:
        session, is_new = await agent._get_or_create_session(phone)
        assert session.id == "sess-ok"
        assert is_new is True
        return "Recovered after session reconnect"

    async def fake_send(phone: str, response: str) -> None:
        sent.append((phone, response))

    async def fake_done(wamid: str) -> None:
        done.append(wamid)

    async def fake_failed(wamid: str, phase: str, error_type: str) -> None:
        failed.append((wamid, phase, error_type))

    monkeypatch.setattr(whatsapp_webhook, "_process_message", fake_process)
    monkeypatch.setattr(whatsapp_webhook, "_send_reply", fake_send)
    monkeypatch.setattr(whatsapp_webhook, "_mark_processed_done", fake_done)
    monkeypatch.setattr(whatsapp_webhook, "_mark_processed_failed", fake_failed)

    await whatsapp_webhook._process_immediate_and_reply(
        "919876543210", "hello", wamid="wamid.retry"
    )

    assert first.list_calls == 2
    assert first.create_calls == 1
    assert sent == [("919876543210", "Recovered after session reconnect")]
    assert done == ["wamid.retry"]
    assert failed == []
