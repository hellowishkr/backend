"""Tests for the admin campaign-sending endpoints (app.api.admin_campaigns).

Covers the admin-auth wiring (require_role) plus thin pass-through to the
already-tested app.services.campaign_sender.send_acquisition_campaign (see
test_send_acquisition_campaign.py for the service's own coverage).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import admin_campaigns
from app.api.admin_campaigns import list_campaign_history, list_campaign_templates, send_campaign
from app.api.admin_campaigns import SendCampaignIn, CampaignRecipientIn
from app.bot.whatsapp_templates import ACQUISITION_TEMPLATE_NAMES
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.models.campaign_context import CampaignContext

TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_token", TOKEN)


@pytest.fixture
async def campaign_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[CampaignContext.__table__])
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def test_list_campaign_templates_returns_acquisition_set():
    result = await list_campaign_templates()
    assert set(result["templates"]) == ACQUISITION_TEMPLATE_NAMES


async def test_send_campaign_dry_run_does_not_write_context(campaign_session_factory):
    async with campaign_session_factory() as db:
        payload = SendCampaignIn(
            template_name="welcome_beta",
            campaign_source="pilot_batch_1",
            recipients=[CampaignRecipientIn(phone="919970073324", name="Prem")],
            dry_run=True,
        )
        result = await send_campaign(payload, db=db)

    assert result["total"] == 1
    assert result["sent"] == 1
    assert result["failed"] == 0

    history = await list_campaign_history(db=db, q=None, page=1, limit=100)
    assert history["total"] == 0


async def test_send_campaign_records_context_on_real_send(campaign_session_factory):
    async with campaign_session_factory() as db:
        with patch(
            "app.services.whatsapp.whatsapp_service.send_template",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
        ) as mock_send:
            payload = SendCampaignIn(
                template_name="welcome_problem_reminder",
                campaign_source="coworkers_pilot_batch_1",
                recipients=[
                    CampaignRecipientIn(phone="919970073324", name="Prem"),
                    CampaignRecipientIn(phone="919981701281", name="Akhilesh"),
                ],
                dry_run=False,
            )
            result = await send_campaign(payload, db=db)

        assert result["sent"] == 2
        assert mock_send.await_count == 2

        history = await list_campaign_history(db=db, q=None, page=1, limit=100)

    assert history["total"] == 2
    phones = {row["phone"] for row in history["history"]}
    assert phones == {"919970073324", "919981701281"}
    assert all(row["campaign_source"] == "coworkers_pilot_batch_1" for row in history["history"])


async def test_send_campaign_rejects_non_acquisition_template(campaign_session_factory):
    async with campaign_session_factory() as db:
        payload = SendCampaignIn(
            template_name="order_confirmation",
            recipients=[CampaignRecipientIn(phone="919970073324", name="Prem")],
        )
        with pytest.raises(Exception):
            await send_campaign(payload, db=db)


async def test_list_campaign_history_filters_by_query(campaign_session_factory):
    async with campaign_session_factory() as db:
        with patch(
            "app.services.whatsapp.whatsapp_service.send_template",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
        ):
            await send_campaign(
                SendCampaignIn(
                    template_name="welcome_beta",
                    recipients=[CampaignRecipientIn(phone="919970073324", name="Prem")],
                ),
                db=db,
            )

        matched = await list_campaign_history(db=db, q="99700", page=1, limit=100)
        assert matched["total"] == 1

        unmatched = await list_campaign_history(db=db, q="00000", page=1, limit=100)
        assert unmatched["total"] == 0


# ─── HTTP-level: verify the require_role guard is actually wired up ───────

@pytest.fixture
def http_client(campaign_session_factory):
    app = FastAPI()
    app.include_router(admin_campaigns.router, prefix="/api")

    async def _override_get_db():
        async with campaign_session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def test_send_endpoint_rejects_missing_admin_token(http_client):
    response = http_client.post(
        "/api/admin/campaigns/send",
        json={"template_name": "welcome_beta", "recipients": [{"phone": "919970073324"}]},
        headers={"X-Admin-Role": "owner"},
    )
    assert response.status_code == 403


def test_send_endpoint_rejects_role_without_access(http_client):
    response = http_client.post(
        "/api/admin/campaigns/send",
        json={"template_name": "welcome_beta", "recipients": [{"phone": "919970073324"}]},
        headers={"X-Admin-Token": TOKEN, "X-Admin-Role": "support"},
    )
    assert response.status_code == 403


def test_send_endpoint_accepts_owner_role(http_client):
    with patch(
        "app.services.whatsapp.whatsapp_service.send_template",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
    ):
        response = http_client.post(
            "/api/admin/campaigns/send",
            json={
                "template_name": "welcome_beta",
                "recipients": [{"phone": "919970073324", "name": "Prem"}],
                "dry_run": True,
            },
            headers={"X-Admin-Token": TOKEN, "X-Admin-Role": "owner"},
        )
    assert response.status_code == 200
    assert response.json()["sent"] == 1
