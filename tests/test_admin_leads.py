import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_leads import (
    ActivityCreatePayload,
    ActivityUpdatePayload,
    LeadUpdatePayload,
    VendorLeadCreatePayload,
    create_activity,
    create_vendor_lead,
    delete_vendor_lead,
    list_activities,
    list_vendor_leads,
    update_activity,
    update_vendor_lead,
)
from app.db.base import Base
from app.models import CorporateLead, LeadActivity, Vendor, VendorLead


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                VendorLead.__table__,
                CorporateLead.__table__,
                LeadActivity.__table__,
                Vendor.__table__,
            ],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_vendor_lead_sets_admin_manual_defaults(session_factory):
    payload = VendorLeadCreatePayload(
        brand_name="Sourced Offline Co",
        contact_name="Founder Name",
        email="Founder@Example.com",
        cities_raw="Pune",
    )
    async with session_factory() as db:
        result = await create_vendor_lead(payload, db)
        rows = (await db.execute(select(VendorLead))).scalars().all()

    assert result["lead"]["source"] == "admin_manual"
    assert result["lead"]["status"] == "new"
    assert len(rows) == 1
    assert rows[0].consent_given is True
    assert rows[0].email == "founder@example.com"  # normalized lowercase


@pytest.mark.asyncio
async def test_create_activity_sets_follow_up_and_records_created_by(session_factory):
    async with session_factory() as db:
        lead_payload = VendorLeadCreatePayload(
            brand_name="Test Brand",
            contact_name="Contact",
            email="contact@example.com",
        )
        created = await create_vendor_lead(lead_payload, db)
        lead_id = created["lead"]["id"]

        activity_payload = ActivityCreatePayload(
            type="call",
            body="Discussed pricing, will share catalog tomorrow.",
            next_follow_up_at="2026-07-01T10:00:00+00:00",
        )
        result = await create_activity(
            "vendor", lead_id, activity_payload, db, x_admin_email="ops@wishkr.com"
        )

        lead = await db.get(VendorLead, lead_id)

    assert result["activity"]["created_by"] == "ops@wishkr.com"
    assert result["activity"]["type"] == "call"
    assert lead.next_follow_up_at is not None


@pytest.mark.asyncio
async def test_create_activity_rejects_unknown_type(session_factory):
    async with session_factory() as db:
        lead_payload = VendorLeadCreatePayload(
            brand_name="Test Brand 2",
            contact_name="Contact",
            email="contact2@example.com",
        )
        created = await create_vendor_lead(lead_payload, db)
        lead_id = created["lead"]["id"]

        with pytest.raises(Exception):
            await create_activity(
                "vendor",
                lead_id,
                ActivityCreatePayload(type="bogus"),
                db,
                x_admin_email="ops@wishkr.com",
            )


@pytest.mark.asyncio
async def test_list_activities_returns_all_for_the_lead(session_factory):
    async with session_factory() as db:
        lead_payload = VendorLeadCreatePayload(
            brand_name="Test Brand 3",
            contact_name="Contact",
            email="contact3@example.com",
        )
        created = await create_vendor_lead(lead_payload, db)
        lead_id = created["lead"]["id"]

        await create_activity(
            "vendor", lead_id, ActivityCreatePayload(type="note", body="first"), db,
            x_admin_email="a@wishkr.com",
        )
        await create_activity(
            "vendor", lead_id, ActivityCreatePayload(type="note", body="second"), db,
            x_admin_email="a@wishkr.com",
        )

        result = await list_activities("vendor", lead_id, db)

    bodies = {a["body"] for a in result["activities"]}
    assert bodies == {"first", "second"}


@pytest.mark.asyncio
async def test_update_vendor_lead_edits_intake_fields(session_factory):
    async with session_factory() as db:
        created = await create_vendor_lead(
            VendorLeadCreatePayload(
                brand_name="Old Name", contact_name="Old Contact", email="old@example.com",
            ),
            db,
        )
        lead_id = created["lead"]["id"]

        result = await update_vendor_lead(
            lead_id,
            LeadUpdatePayload(
                brand_name="New Name",
                contact_name="New Contact",
                phone="+91 90000 00000",
                categories=["candles", "hampers"],
                notes="<p>Updated note</p>",
            ),
            db,
        )

    lead = result["lead"]
    assert lead["brand_name"] == "New Name"
    assert lead["contact_name"] == "New Contact"
    assert lead["phone"] == "+91 90000 00000"
    assert lead["categories"] == ["candles", "hampers"]
    assert lead["notes"] == "<p>Updated note</p>"


@pytest.mark.asyncio
async def test_update_vendor_lead_accepts_junk_status(session_factory):
    async with session_factory() as db:
        created = await create_vendor_lead(
            VendorLeadCreatePayload(
                brand_name="Junky", contact_name="Cee", email="j@example.com",
            ),
            db,
        )
        lead_id = created["lead"]["id"]

        result = await update_vendor_lead(lead_id, LeadUpdatePayload(status="junk"), db)

    assert result["lead"]["status"] == "junk"


@pytest.mark.asyncio
async def test_delete_vendor_lead_soft_deletes_and_hides_from_list(session_factory):
    async with session_factory() as db:
        created = await create_vendor_lead(
            VendorLeadCreatePayload(
                brand_name="To Delete", contact_name="Contact", email="delete@example.com",
            ),
            db,
        )
        lead_id = created["lead"]["id"]

        delete_result = await delete_vendor_lead(lead_id, db)
        listing = await list_vendor_leads(db)
        lead = await db.get(VendorLead, lead_id)

    assert delete_result["success"] is True
    assert lead.deleted_at is not None
    assert all(l["id"] != lead_id for l in listing["leads"])


@pytest.mark.asyncio
async def test_delete_vendor_lead_404_on_already_deleted(session_factory):
    async with session_factory() as db:
        created = await create_vendor_lead(
            VendorLeadCreatePayload(
                brand_name="Twice Deleted", contact_name="Contact", email="twice@example.com",
            ),
            db,
        )
        lead_id = created["lead"]["id"]
        await delete_vendor_lead(lead_id, db)

        with pytest.raises(Exception):
            await delete_vendor_lead(lead_id, db)


@pytest.mark.asyncio
async def test_update_activity_edits_body_and_syncs_latest_follow_up(session_factory):
    async with session_factory() as db:
        created = await create_vendor_lead(
            VendorLeadCreatePayload(
                brand_name="Follow Up Co", contact_name="Contact", email="followup@example.com",
            ),
            db,
        )
        lead_id = created["lead"]["id"]

        activity_result = await create_activity(
            "vendor",
            lead_id,
            ActivityCreatePayload(type="call", body="<p>Original</p>", next_follow_up_at="2026-07-01T10:00:00+00:00"),
            db,
            x_admin_email="ops@wishkr.com",
        )
        activity_id = activity_result["activity"]["id"]

        update_result = await update_activity(
            "vendor",
            lead_id,
            activity_id,
            ActivityUpdatePayload(body="<p>Corrected note</p>", next_follow_up_at="2026-07-02T11:00:00+00:00"),
            db,
        )

        lead = await db.get(VendorLead, lead_id)

    assert update_result["activity"]["body"] == "<p>Corrected note</p>"
    assert lead.next_follow_up_at.replace(tzinfo=None).isoformat() == "2026-07-02T11:00:00"
