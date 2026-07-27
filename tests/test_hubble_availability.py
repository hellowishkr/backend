from types import SimpleNamespace
from unittest.mock import AsyncMock
import os

import httpx
import pytest

import google.adk.sessions as _adk_sessions


class _StubSessionService:
    def __init__(self, **_kw):
        pass


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

os.environ["SENTRY_DSN"] = "http://public@example.com/1"

import app.core.observability as _observability
_real_init_sentry = _observability.init_sentry
_observability.init_sentry = lambda _settings: False
from app import main
_observability.init_sentry = _real_init_sentry
from app.bot.tools.tool_impl import gifting
from app.models import PurchaseStatus
from app.services import hubble_fulfillment
from app.services.hubble import HubbleService, HubbleUnavailableError


class _DummyApp:
    def __init__(self):
        self.state = SimpleNamespace()


class _Auth404Client:
    async def post(self, *_args, **_kwargs):
        request = httpx.Request("POST", "https://api.myhubble.money/v1/partners/auth/login")
        response = httpx.Response(404, request=request, json={"error": "not found"})
        return response


@pytest.fixture
def hubble_settings(monkeypatch):
    monkeypatch.setattr("app.services.hubble.settings.hubble_enabled", True)
    monkeypatch.setattr("app.services.hubble.settings.hubble_env", "production")
    monkeypatch.setattr("app.services.hubble.settings.hubble_effective_client_id", "client-id", raising=False)
    monkeypatch.setattr("app.services.hubble.settings.hubble_effective_client_secret", "super-secret", raising=False)
    monkeypatch.setattr("app.services.hubble.settings.hubble_effective_base_url", "https://api.myhubble.money", raising=False)


@pytest.mark.asyncio
async def test_lifespan_continues_when_hubble_auth_404_and_fail_startup_false(monkeypatch):
    app = _DummyApp()

    monkeypatch.setattr(main, "_validate_config", lambda: None)
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "hubble_enabled", True)
    monkeypatch.setattr(main.settings, "hubble_fail_startup_on_auth_error", False)
    monkeypatch.setattr(main.settings, "run_scheduler", False)

    import app.services.hubble as hubble_mod
    monkeypatch.setattr(
        hubble_mod.hubble_service,
        "authenticate",
        AsyncMock(side_effect=HubbleUnavailableError("Hubble auth failed with HTTP 404")),
    )

    import app.webhooks.whatsapp as wa
    monkeypatch.setattr(wa, "drain_active_tasks", AsyncMock())

    async with main.lifespan(app):
        assert getattr(app.state, "scheduler_started", True) is False


@pytest.mark.asyncio
async def test_lifespan_raises_when_hubble_fail_startup_true(monkeypatch):
    app = _DummyApp()

    monkeypatch.setattr(main, "_validate_config", lambda: None)
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "hubble_enabled", True)
    monkeypatch.setattr(main.settings, "hubble_fail_startup_on_auth_error", True)
    monkeypatch.setattr(main.settings, "run_scheduler", False)

    import app.services.hubble as hubble_mod
    monkeypatch.setattr(
        hubble_mod.hubble_service,
        "authenticate",
        AsyncMock(side_effect=HubbleUnavailableError("Hubble auth failed with HTTP 404")),
    )

    with pytest.raises(HubbleUnavailableError):
        async with main.lifespan(app):
            pass


@pytest.mark.asyncio
async def test_health_returns_ok_when_hubble_unavailable():
    assert await main.health() == {"status": "ok"}


def _set_valid_production_config(
    monkeypatch,
    *,
    app_role: str = "web",
    gift_refinement_enabled: bool = True,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "app_role", app_role)
    monkeypatch.setattr(main.settings, "hubble_enabled", False)
    monkeypatch.setattr(
        main.settings,
        "gift_refinement_enabled",
        gift_refinement_enabled,
    )
    monkeypatch.setattr(main.settings, "allowed_hosts", "api.wishkr.com")
    for key in [
        "gemini_api_key",
        "whatsapp_access_token",
        "whatsapp_phone_number_id",
        "whatsapp_verify_token",
        "whatsapp_webhook_secret",
        "razorpay_key_id",
        "razorpay_key_secret",
        "razorpay_webhook_secret",
        "founder_phone",
    ]:
        monkeypatch.setattr(
            main.settings,
            key,
            "919999999999" if key == "founder_phone" else "set",
        )


def test_validate_config_does_not_require_hubble_fields_when_disabled(monkeypatch):
    _set_valid_production_config(monkeypatch)
    monkeypatch.setattr(main.settings, "hubble_client_id", "")
    monkeypatch.setattr(main.settings, "hubble_client_secret", "")
    monkeypatch.setattr(main.settings, "hubble_customer_phone", "")

    main._validate_config()


def test_validate_config_requires_gift_refinement_on_production_web(monkeypatch):
    _set_valid_production_config(monkeypatch, gift_refinement_enabled=False)

    with pytest.raises(RuntimeError, match="GIFT_REFINEMENT_ENABLED must be true"):
        main._validate_config()


def test_validate_config_allows_disabled_refinement_on_production_worker(monkeypatch):
    _set_valid_production_config(
        monkeypatch,
        app_role="scheduler",
        gift_refinement_enabled=False,
    )

    main._validate_config()


@pytest.mark.asyncio
async def test_hubble_get_brands_returns_integration_unavailable_when_disabled(monkeypatch):
    monkeypatch.setattr("app.services.hubble.settings.hubble_enabled", False)
    svc = HubbleService()

    with pytest.raises(HubbleUnavailableError) as exc:
        await svc.get_brands()

    assert exc.value.response == {
        "status": "integration_unavailable",
        "provider": "hubble",
        "reason": "Hubble integration is disabled",
    }


@pytest.mark.asyncio
async def test_hubble_operation_returns_integration_unavailable_when_auth_fails(hubble_settings, caplog):
    svc = HubbleService()
    svc._client = _Auth404Client()

    with pytest.raises(HubbleUnavailableError) as exc:
        await svc.create_order(
            product_id="brand_1",
            amount=100,
            denomination=100,
            quantity=1,
            reference_id="pur_1",
            customer_name="Wishkr Order pur_1",
            customer_phone="9999999999",
        )

    assert exc.value.response == {
        "status": "integration_unavailable",
        "provider": "hubble",
        "reason": "Hubble auth failed with HTTP 404",
    }
    assert "super-secret" not in caplog.text
    assert "Bearer" not in caplog.text


@pytest.mark.asyncio
async def test_hubble_catalog_tool_returns_integration_unavailable(monkeypatch):
    async def unavailable():
        raise HubbleUnavailableError("Hubble integration is disabled")

    monkeypatch.setattr(gifting.hubble_service, "get_brands", unavailable)

    result = await gifting.search_brands(None, "919999999999", {"query": "amazon"})

    assert result == {
        "status": "integration_unavailable",
        "provider": "hubble",
        "reason": "Hubble integration is disabled",
    }


@pytest.mark.asyncio
async def test_hubble_delivery_does_not_mark_purchase_delivered_when_auth_unavailable(monkeypatch):
    purchase = SimpleNamespace(
        id="pur_test",
        hubble_product_id="brand_1",
        amount=100,
        hubble_order_id=None,
        provider=None,
        provider_product_id=None,
        provider_order_id=None,
        voucher_code=None,
        voucher_pin=None,
        voucher_expiry=None,
        status=PurchaseStatus.PENDING,
    )

    monkeypatch.setattr(hubble_fulfillment.settings, "mvp_reminder_only", False)
    monkeypatch.setattr(hubble_fulfillment.settings, "hubble_customer_phone", "919999999999")
    monkeypatch.setattr(hubble_fulfillment, "_validate_denomination", AsyncMock())
    monkeypatch.setattr(
        hubble_fulfillment.hubble_service,
        "create_order",
        AsyncMock(side_effect=HubbleUnavailableError("Hubble auth failed with HTTP 404")),
    )

    with pytest.raises(hubble_fulfillment.HubbleOrderError):
        await hubble_fulfillment.issue_hubble_voucher(purchase)

    assert purchase.status == PurchaseStatus.PENDING
    assert purchase.voucher_code is None
    assert purchase.hubble_order_id is None


@pytest.mark.asyncio
async def test_hubble_voucher_issuance_disabled_in_mvp(monkeypatch):
    purchase = SimpleNamespace(
        id="pur_mvp",
        hubble_product_id="brand_1",
        amount=100,
        status=PurchaseStatus.PENDING,
    )

    monkeypatch.setattr(hubble_fulfillment.settings, "mvp_reminder_only", True)
    create_order = AsyncMock()
    monkeypatch.setattr(hubble_fulfillment.hubble_service, "create_order", create_order)

    with pytest.raises(hubble_fulfillment.HubbleOrderError) as exc:
        await hubble_fulfillment.issue_hubble_voucher(purchase)

    assert str(exc.value) == "mvp_reminder_only"
    create_order.assert_not_called()
