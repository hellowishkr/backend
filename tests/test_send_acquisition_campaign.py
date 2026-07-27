"""Tests for POST /internal/send-acquisition-campaign and the underlying
app.services.campaign_sender.send_acquisition_campaign.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import internal
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.models import CampaignContext
from app.services.campaign_sender import (
    CampaignRecipient,
    InvalidCampaignTemplate,
    TooManyRecipients,
    send_acquisition_campaign,
)

TOKEN = "test-internal-token"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[CampaignContext.__table__])
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db: AsyncSession) -> TestClient:
    app = FastAPI()
    app.include_router(internal.router)

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _mock_send_template(**_kw):
    return AsyncMock(return_value={"messages": [{"id": "wamid.test"}]})


# ─── Service-level tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_campaign_sends_and_records_context(db: AsyncSession):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()) as mock_send:
        result = await send_acquisition_campaign(
            db,
            template_name="welcome_problem_duplicate",
            recipients=[
                CampaignRecipient(phone="918237324467", name="Karan"),
                CampaignRecipient(phone="919876500099", name="Aditi"),
            ],
            campaign_source="coworker_test_july_2026",
        )

    assert result.total == 2
    assert result.sent == 2
    assert result.failed == 0
    assert result.skipped == 0
    assert mock_send.await_count == 2

    ctx = (await db.execute(
        select(CampaignContext).where(CampaignContext.phone == "918237324467")
    )).scalar_one()
    assert ctx.template_name == "welcome_problem_duplicate"
    assert ctx.campaign_source == "coworker_test_july_2026"
    assert ctx.consumed_at is None


@pytest.mark.asyncio
async def test_invalid_template_rejected(db: AsyncSession):
    with pytest.raises(InvalidCampaignTemplate):
        await send_acquisition_campaign(
            db,
            template_name="order_confirmation",  # transactional, not acquisition
            recipients=[CampaignRecipient(phone="918237324467", name="Karan")],
        )


@pytest.mark.asyncio
async def test_duplicate_phone_in_request_is_skipped(db: AsyncSession):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()) as mock_send:
        result = await send_acquisition_campaign(
            db,
            template_name="welcome_beta",
            recipients=[
                CampaignRecipient(phone="918237324467", name="Karan"),
                CampaignRecipient(phone="91-8237-324-467", name="Karan again"),  # same number, different formatting
            ],
        )

    assert result.total == 2
    assert result.sent == 1
    assert result.skipped == 1
    assert result.errors[0]["reason"] == "duplicate_in_request"
    assert mock_send.await_count == 1


@pytest.mark.asyncio
async def test_dry_run_does_not_call_meta_or_record_context(db: AsyncSession):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()) as mock_send:
        result = await send_acquisition_campaign(
            db,
            template_name="welcome_curiosity",
            recipients=[CampaignRecipient(phone="918237324467", name="Karan")],
            dry_run=True,
        )

    assert result.sent == 1
    mock_send.assert_not_awaited()

    ctx = (await db.execute(
        select(CampaignContext).where(CampaignContext.phone == "918237324467")
    )).scalar_one_or_none()
    assert ctx is None


@pytest.mark.asyncio
async def test_failed_meta_send_does_not_record_context(db: AsyncSession):
    with patch(
        "app.services.whatsapp.whatsapp_service.send_template",
        new=AsyncMock(side_effect=RuntimeError("Meta 401")),
    ):
        result = await send_acquisition_campaign(
            db,
            template_name="welcome_problem_lastminute",
            recipients=[CampaignRecipient(phone="918237324467", name="Karan")],
        )

    assert result.sent == 0
    assert result.failed == 1
    assert result.errors[0]["phone"] == "918237324467"

    ctx = (await db.execute(
        select(CampaignContext).where(CampaignContext.phone == "918237324467")
    )).scalar_one_or_none()
    assert ctx is None


@pytest.mark.asyncio
async def test_invalid_phone_is_skipped_not_sent(db: AsyncSession):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()) as mock_send:
        result = await send_acquisition_campaign(
            db,
            template_name="welcome_beta",
            recipients=[CampaignRecipient(phone="123", name="Bad Number")],
        )

    assert result.skipped == 1
    assert result.errors[0]["reason"] == "invalid_phone"
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_many_recipients_rejected(db: AsyncSession):
    recipients = [CampaignRecipient(phone=f"9198765{str(i).zfill(5)}") for i in range(51)]
    with pytest.raises(TooManyRecipients):
        await send_acquisition_campaign(
            db, template_name="welcome_beta", recipients=recipients,
        )


# ─── Endpoint-level tests ──────────────────────────────────────────────────

def test_endpoint_requires_internal_token(client: TestClient):
    response = client.post(
        "/internal/send-acquisition-campaign",
        json={
            "template_name": "welcome_beta",
            "recipients": [{"phone": "918237324467", "name": "Karan"}],
        },
    )
    assert response.status_code == 403


def test_endpoint_sends_campaign_and_returns_summary(client: TestClient):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()):
        response = client.post(
            "/internal/send-acquisition-campaign",
            headers={"X-Internal-Token": TOKEN},
            json={
                "template_name": "welcome_problem_duplicate",
                "campaign_source": "coworker_test_july_2026",
                "recipients": [
                    {"phone": "918237324467", "name": "Karan"},
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["sent"] == 1
    assert body["failed"] == 0
    assert body["skipped"] == 0
    assert body["errors"] == []


def test_endpoint_rejects_non_acquisition_template(client: TestClient):
    response = client.post(
        "/internal/send-acquisition-campaign",
        headers={"X-Internal-Token": TOKEN},
        json={
            "template_name": "payment_failed",
            "recipients": [{"phone": "918237324467", "name": "Karan"}],
        },
    )
    assert response.status_code == 422


def test_endpoint_rejects_over_50_recipients(client: TestClient):
    recipients = [{"phone": f"9198765{str(i).zfill(5)}"} for i in range(51)]
    response = client.post(
        "/internal/send-acquisition-campaign",
        headers={"X-Internal-Token": TOKEN},
        json={"template_name": "welcome_beta", "recipients": recipients},
    )
    assert response.status_code == 422


def test_endpoint_dry_run_returns_preview_without_sending(client: TestClient):
    with patch("app.services.whatsapp.whatsapp_service.send_template", new=_mock_send_template()) as mock_send:
        response = client.post(
            "/internal/send-acquisition-campaign",
            headers={"X-Internal-Token": TOKEN},
            json={
                "template_name": "welcome_beta",
                "recipients": [{"phone": "918237324467", "name": "Karan"}],
                "dry_run": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["sent"] == 1
    mock_send.assert_not_awaited()
