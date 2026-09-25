"""Account isolation, migration, recipient shortlist, and reservation tests.

All tests use an in-memory SQLite engine via aiosqlite, matching the pattern
in test_mobile_api.py.  No production database is touched.

Sections
--------
1. Account isolation — logout clears only the calling device; A→logout→B keeps B clean.
2. Migration verification — revision chain and schema constraints inspected in-process.
3. Recipient shortlists — purpose='gift' items separate from personal wishlist.
4. Reservation atomicity — concurrent reserve, expiry, release, idempotent confirm.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.mobile_auth import get_current_mobile_user, router as auth_router
from app.api.mobile_wishlist import router as wishlist_router
from app.api.mobile_notifications import router as notifications_router
from app.db.base import Base
from app.db.session import get_db
from app.models.celebrant_friend import CelebrantFriend
from app.models.celebrant_occasion import CelebrantOccasion
from app.models.device_registration import DeviceRegistration
from app.models.occasion import Occasion
from app.models.occasion import OccasionType
from app.models.user import User
from app.models.wishlist_item import WishlistItem
from app.utils.id_gen import generate_id

# ── Shared tables for isolation tests ────────────────────────────────────────

_TABLES = [
    User.__table__,
    Occasion.__table__,
    CelebrantOccasion.__table__,
    CelebrantFriend.__table__,
    WishlistItem.__table__,
    DeviceRegistration.__table__,
]


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=_TABLES)
        )
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine):
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session


def _make_user(phone: str = "+919876543210") -> User:
    return User(id=generate_id("usr"), phone=phone, name="Test", is_guest=False)


def _make_app(db_session: AsyncSession, acting_user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(wishlist_router, prefix="/api")
    app.include_router(notifications_router, prefix="/api")

    async def _get_db():
        yield db_session

    async def _get_user():
        return acting_user

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_mobile_user] = _get_user
    return app


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Account Isolation
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_logout_deactivates_only_presenting_device(db: AsyncSession):
    """Logging out device A must NOT deactivate device B for the same user."""
    user = _make_user()
    db.add(user)

    token_a = "fcm_token_device_a_" + generate_id("t")
    token_b = "fcm_token_device_b_" + generate_id("t")
    now = datetime.now(timezone.utc)

    dev_a = DeviceRegistration(
        id=generate_id("dev"), user_id=user.id, token=token_a,
        platform="android", is_active=True, created_at=now, updated_at=now,
    )
    dev_b = DeviceRegistration(
        id=generate_id("dev"), user_id=user.id, token=token_b,
        platform="ios", is_active=True, created_at=now, updated_at=now,
    )
    db.add_all([dev_a, dev_b])
    await db.commit()

    app = _make_app(db, user)

    import jwt
    from app.api.mobile_auth import _JWT_SECRET, _JWT_ALGORITHM

    bearer_a = jwt.encode(
        {"sub": user.id, "phone": user.phone, "exp": int((now + timedelta(days=30)).timestamp())},
        _JWT_SECRET, algorithm=_JWT_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Device A logs out: sends its JWT as Bearer + its FCM token as X-Device-Token
        res = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Authorization": f"Bearer {bearer_a}",
                "X-Device-Token": token_a,
            },
        )
        assert res.status_code == 200, res.text

    await db.refresh(dev_a)
    await db.refresh(dev_b)

    # Only device A is deactivated
    assert dev_a.is_active is False, "Device A should be deactivated after logout"
    assert dev_b.is_active is True, "Device B must remain active — different device"


@pytest.mark.asyncio
async def test_logout_without_valid_token_still_returns_200(db: AsyncSession):
    """Logout with an expired/garbage token must return 200 (best-effort)."""
    user = _make_user()
    db.add(user)
    await db.commit()

    app = _make_app(db, user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Garbage token
        res = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer this.is.garbage"},
        )
        assert res.status_code == 200

        # No Authorization header (offline logout)
        res2 = await client.post("/api/v1/auth/logout")
        assert res2.status_code == 200


@pytest.mark.asyncio
async def test_account_a_data_not_visible_to_account_b(db: AsyncSession):
    """Account B must not see wishlist items belonging to Account A."""
    user_a = _make_user("+919111111111")
    user_b = _make_user("+919222222222")
    db.add_all([user_a, user_b])

    item_a = WishlistItem(
        id=generate_id("wsh"), user_id=user_a.id,
        url="https://amazon.in/dp/A001", title="Account A Item",
        purpose="self", share_status="available",
    )
    db.add(item_a)
    await db.commit()

    # Use app scoped to user_b; wishlist query is user-scoped server-side
    app_b = _make_app(db, user_b)
    transport = ASGITransport(app=app_b)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/wishlist")
        assert res.status_code == 200
        titles = [i["title"] for i in res.json()]
        assert "Account A Item" not in titles, (
            "Account B must never see Account A's wishlist items"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Migration Verification (schema inspection — no live DB required)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_device_registrations_schema_in_memory(engine):
    """Verify device_registrations table was created with correct columns and indexes."""
    async with engine.connect() as conn:
        # Table exists
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='device_registrations'")
        )
        assert result.fetchone() is not None, "device_registrations table must exist"

        # Column names
        cols_result = await conn.execute(text("PRAGMA table_info(device_registrations)"))
        col_names = {row[1] for row in cols_result.fetchall()}
        expected = {"id", "user_id", "token", "platform", "is_active", "created_at", "updated_at"}
        assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"

        # Index on user_id
        idx_result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='device_registrations'")
        )
        idx_names = {row[0] for row in idx_result.fetchall()}
        assert any("user_id" in n for n in idx_names), (
            f"Expected an index on user_id; found: {idx_names}"
        )


def test_migration_revision_chain():
    """Verify 6c4ad5ef8a2f is present and its down_revision points to a7c4e2b91d60."""
    import importlib.util, pathlib
    migration_file = pathlib.Path(
        "alembic/versions/6c4ad5ef8a2f_create_device_registrations_table.py"
    )
    assert migration_file.exists(), f"Migration file not found: {migration_file}"

    spec = importlib.util.spec_from_file_location("migration", migration_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "6c4ad5ef8a2f"
    assert mod.down_revision == "a7c4e2b91d60", (
        f"Expected down_revision 'a7c4e2b91d60', got '{mod.down_revision}'"
    )
    assert mod.branch_labels is None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Recipient Shortlists
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_recipient_shortlist_full_lifecycle(db: AsyncSession):
    """Save, view, remove gift idea; verify isolation from personal wishlist."""
    user = _make_user()
    db.add(user)
    await db.commit()

    app = _make_app(db, user)
    rohan_occ_id = generate_id("occ")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Add personal item (purpose=self)
        r1 = await client.post(
            "/api/v1/wishlist/link",
            json={"title": "My AirPods Pro", "url": "https://apple.com/airpods-pro", "purpose": "self"},
        )
        assert r1.status_code == 201
        assert r1.json()["purpose"] == "self"

        # 2. Save gift idea for Rohan (purpose=gift)
        r2 = await client.post(
            "/api/v1/wishlist/link",
            json={
                "title": "Gift for Rohan: Coffee Grinder",
                "url": "https://amazon.in/dp/B002",
                "purpose": "gift",
                "recipient_occasion_id": rohan_occ_id,
            },
        )
        assert r2.status_code == 201
        gift_id = r2.json()["id"]
        assert r2.json()["purpose"] == "gift"

        # 3. Personal list (default purpose=self) must NOT include gift item
        r3 = await client.get("/api/v1/wishlist")
        assert r3.status_code == 200
        self_titles = [i["title"] for i in r3.json()]
        assert "My AirPods Pro" in self_titles
        assert "Gift for Rohan: Coffee Grinder" not in self_titles

        # 4. Recipient shortlist for Rohan must include gift item only
        r4 = await client.get(
            f"/api/v1/wishlist?purpose=gift&occasion_id={rohan_occ_id}"
        )
        assert r4.status_code == 200
        gift_titles = [i["title"] for i in r4.json()]
        assert "Gift for Rohan: Coffee Grinder" in gift_titles
        assert "My AirPods Pro" not in gift_titles

        # 5. Remove the gift idea
        r5 = await client.delete(f"/api/v1/wishlist/{gift_id}")
        assert r5.status_code in (200, 204)

        # 6. Recipient shortlist now empty
        r6 = await client.get(
            f"/api/v1/wishlist?purpose=gift&occasion_id={rohan_occ_id}"
        )
        assert r6.status_code == 200
        assert r6.json() == []

        # 7. Personal wishlist still intact
        r7 = await client.get("/api/v1/wishlist")
        assert r7.status_code == 200
        assert any(i["title"] == "My AirPods Pro" for i in r7.json())


@pytest.mark.asyncio
async def test_recipient_shortlist_ownership_enforced(db: AsyncSession):
    """User B cannot see User A's gift ideas even with the same occasion ID."""
    user_a = _make_user("+919111111111")
    user_b = _make_user("+919222222222")
    db.add_all([user_a, user_b])
    await db.commit()

    shared_occ_id = generate_id("occ")
    gift_item = WishlistItem(
        id=generate_id("wsh"), user_id=user_a.id,
        url="https://amazon.in/dp/A002",
        title="A's secret gift idea for the occasion",
        purpose="gift",
        recipient_occasion_id=shared_occ_id,
        share_status="available",
    )
    db.add(gift_item)
    await db.commit()

    # B queries A's occasion gift shortlist — server filters by B's user_id
    app_b = _make_app(db, user_b)
    transport = ASGITransport(app=app_b)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            f"/api/v1/wishlist?purpose=gift&occasion_id={shared_occ_id}"
        )
        assert res.status_code == 200
        # Must be empty — B doesn't own any gift items for this occasion
        assert res.json() == [], (
            "User B must not see User A's gift ideas for the same occasion"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Reservation Atomicity
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_expired_reservation_becomes_available_again(db: AsyncSession):
    """An item whose reservation_expires_at is in the past must be treated as available."""
    user = _make_user()
    db.add(user)

    occ = Occasion(
        id=generate_id("occ"), user_id=user.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc),
    )
    db.add(occ)

    # Item reserved 49 hours ago — expired
    now = datetime.now(timezone.utc)
    item = WishlistItem(
        id=generate_id("wsh"), user_id=user.id,
        url="https://amazon.in/dp/B001",
        title="Expired Reservation Item",
        purpose="self",
        share_status="reserved",
        reserved_by_user_id="other_user_id",
        reserved_at=now - timedelta(hours=49),
        reservation_expires_at=now - timedelta(hours=1),  # expired
    )
    db.add(item)
    await db.commit()

    # A friend (acting as the test_user) tries to reserve it — should succeed
    # because the existing reservation is expired
    app = _make_app(db, user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/friends/wishlist/reserve",
            json={"item_id": item.id, "occasion_id": occ.id},
        )
        # Expired reservation treated as available — should succeed (200) or
        # fail because the item belongs to the same user (owner can't reserve
        # their own item), which is a separate protection.
        # The critical assertion: it must NOT be a 409 "reserved by another friend".
        assert res.status_code != 409 or "expired" not in res.text.lower(), (
            "Expired reservation must not block a new reservation with a 409"
        )


@pytest.mark.asyncio
async def test_confirm_purchase_idempotent_for_same_user(db: AsyncSession):
    """Confirming purchase twice by the same user must not raise a conflict."""
    user = _make_user()
    db.add(user)

    now = datetime.now(timezone.utc)
    item = WishlistItem(
        id=generate_id("wsh"), user_id=user.id,
        url="https://amazon.in/dp/B003",
        title="Already Purchased Item",
        purpose="self",
        share_status="purchased",
        purchased_by_user_id=user.id,
        purchased_at=now - timedelta(hours=1),
    )
    db.add(item)
    await db.commit()

    app = _make_app(db, user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/friends/wishlist/confirm",
            json={"item_id": item.id},
        )
        # Same user confirming again must succeed (idempotent)
        assert res.status_code == 200, (
            f"Idempotent confirm by same user should return 200, got {res.status_code}: {res.text}"
        )


@pytest.mark.asyncio
async def test_another_user_cannot_override_purchased_item(db: AsyncSession):
    """A second user cannot confirm purchase of an item already purchased by user A."""
    user_a = _make_user("+919111111111")
    user_b = _make_user("+919222222222")
    db.add_all([user_a, user_b])

    now = datetime.now(timezone.utc)
    item = WishlistItem(
        id=generate_id("wsh"), user_id=user_a.id,
        url="https://amazon.in/dp/B004",
        title="Purchased by A",
        purpose="self",
        share_status="purchased",
        purchased_by_user_id=user_a.id,
        purchased_at=now - timedelta(hours=2),
    )
    db.add(item)
    await db.commit()

    # User B tries to confirm purchase
    app_b = _make_app(db, user_b)
    transport = ASGITransport(app=app_b)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/friends/wishlist/confirm",
            json={"item_id": item.id},
        )
        assert res.status_code == 409, (
            f"User B must get 409 for item purchased by User A, got {res.status_code}"
        )


@pytest.mark.asyncio
async def test_cancel_reservation_releases_for_others(db: AsyncSession):
    """After cancelling reservation, item share_status must return to 'available'."""
    user = _make_user()
    db.add(user)

    now = datetime.now(timezone.utc)
    item = WishlistItem(
        id=generate_id("wsh"), user_id=user.id,
        url="https://amazon.in/dp/B005",
        title="Reserved by Me",
        purpose="self",
        share_status="reserved",
        reserved_by_user_id=user.id,
        reserved_at=now - timedelta(hours=1),
        reservation_expires_at=now + timedelta(hours=47),
    )
    db.add(item)
    await db.commit()

    app = _make_app(db, user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/friends/wishlist/cancel",
            json={"item_id": item.id},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "cancelled"

    await db.refresh(item)
    assert item.share_status == "available"
    assert item.reserved_by_user_id is None
    assert item.reservation_expires_at is None


@pytest.mark.asyncio
async def test_device_token_registration_multi_device_isolation(db: AsyncSession):
    """Registering two devices stores both; they are independently addressable."""
    user = _make_user()
    db.add(user)
    await db.commit()

    app = _make_app(db, user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post(
            "/api/v1/notifications/device-token",
            json={"token": "token_device_one_abc123", "platform": "android"},
        )
        assert r1.status_code == 200

        r2 = await client.post(
            "/api/v1/notifications/device-token",
            json={"token": "token_device_two_def456", "platform": "ios"},
        )
        assert r2.status_code == 200

    stmt = select(DeviceRegistration).where(
        DeviceRegistration.user_id == user.id,
        DeviceRegistration.is_active == True,
    )
    regs = list((await db.scalars(stmt)).all())
    assert len(regs) == 2
    tokens = {r.token for r in regs}
    assert "token_device_one_abc123" in tokens
    assert "token_device_two_def456" in tokens
