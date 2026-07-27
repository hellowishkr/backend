import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api import corporate_leads
from app.api.corporate_leads import CorporateLeadIn, submit_corporate_lead
from app.db.base import Base
from app.models import CorporateLead, HoneypotHit


@pytest.fixture
async def corporate_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[CorporateLead.__table__, HoneypotHit.__table__],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/corporate/leads",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
        }
    )


def _payload() -> CorporateLeadIn:
    return CorporateLeadIn(
        company_name="Acme People Ops",
        contact_name="Nisha Rao",
        email="nisha@example.com",
        phone="+91 98765 43210",
        role="People Ops",
        recipient_count=120,
        occasion="Diwali",
        budget_per_recipient="Rs 1500-2500",
        timeline="October",
        city="Bangalore",
        notes="Needs curated desk drops.",
        consent_given=True,
        source="website",
    )


@pytest.mark.asyncio
async def test_corporate_lead_persists_and_emails_hello(
    corporate_session_factory, monkeypatch
):
    sent: list[dict] = []
    async def fake_notify(lead):
        sent.append(
            {
                "to_email": "hello@wishkr.com",
                "reply_to": lead.email,
                "subject": f"Corporate gifting lead: {lead.company_name}",
            }
        )

    monkeypatch.setattr(corporate_leads, "_notify_hello", fake_notify)

    async with corporate_session_factory() as db:
        result = await submit_corporate_lead(_payload(), _request(), db)
        rows = (await db.execute(select(CorporateLead))).scalars().all()

    assert result["success"] is True
    assert len(rows) == 1
    assert rows[0].company_name == "Acme People Ops"
    assert rows[0].email == "nisha@example.com"

    await corporate_leads.asyncio.sleep(0)
    assert sent[0]["to_email"] == "hello@wishkr.com"
    assert sent[0]["reply_to"] == "nisha@example.com"
    assert "Acme People Ops" in sent[0]["subject"]


@pytest.mark.asyncio
async def test_corporate_lead_honeypot_does_not_create_lead(corporate_session_factory):
    payload = _payload()
    payload.website_url = "https://bot.example"

    async with corporate_session_factory() as db:
        result = await submit_corporate_lead(payload, _request(), db)
        leads = (await db.execute(select(CorporateLead))).scalars().all()
        hits = (await db.execute(select(HoneypotHit))).scalars().all()

    assert result["success"] is True
    assert leads == []
    assert len(hits) == 1
