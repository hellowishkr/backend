"""Integration tests for Wishkr Mobile REST Endpoints.

Tests run against an in-memory SQLite database via aiosqlite, overriding
the FastAPI database dependency and mobile JWT user authentication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.mobile_auth import get_current_mobile_user
from app.api.mobile_calendar import router as calendar_router
from app.api.mobile_discovery import router as discovery_router
from app.api.mobile_notifications import router as notifications_router
from app.api.mobile_occasions import router as occasions_router
from app.api.mobile_preferences import router as preferences_router
from app.api.mobile_wishlist import router as wishlist_router
from app.db.base import Base
from app.db.session import get_db
from app.models import (
    CelebrantFriend,
    CelebrantOccasion,
    Contact,
    GiftLog,
    Occasion,
    PreferenceRequest,
    RecipientPreference,
    PreferenceRequest,
    GiftLog,
    DeviceRegistration,
    User,
    WishlistItem,
)
from app.models.occasion import OccasionType
from app.utils.id_gen import generate_id

_TABLES = [
    User.__table__,
    Contact.__table__,
    Occasion.__table__,
    WishlistItem.__table__,
    CelebrantOccasion.__table__,
    CelebrantFriend.__table__,
    RecipientPreference.__table__,
    PreferenceRequest.__table__,
    GiftLog.__table__,
    DeviceRegistration.__table__,
]


@pytest.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def test_user(async_db: AsyncSession) -> User:
    user = User(
        id=generate_id("usr"),
        phone="+919876543210",
        name="Test Mobile User",
        is_guest=False,
    )
    async_db.add(user)
    await async_db.commit()
    await async_db.refresh(user)
    return user


@pytest.fixture
def test_app(async_db: AsyncSession, test_user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(occasions_router, prefix="/api")
    app.include_router(wishlist_router, prefix="/api")
    app.include_router(preferences_router, prefix="/api")
    app.include_router(calendar_router, prefix="/api")
    app.include_router(notifications_router, prefix="/api")
    app.include_router(discovery_router, prefix="/api")

    async def override_get_db():
        yield async_db

    async def override_get_user():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_mobile_user] = override_get_user
    return app


@pytest.mark.asyncio
async def test_mobile_preferences_crud(test_app: FastAPI):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Update preferences
        res = await client.put(
            "/api/v1/preferences",
            json={
                "categories": ["Coffee Lover", "Gamer", "Minimalist"],
                "preferred_budget": "₹1,000 – ₹3,000",
                "notes": "Love dark roast coffee beans!",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert "Coffee Lover" in data["preferred_categories"]
        assert data["notes"] == "Love dark roast coffee beans!"

        # 2. Retrieve preferences
        get_res = await client.get("/api/v1/preferences")
        assert get_res.status_code == 200
        assert get_res.json()["preferred_categories"] == ["Coffee Lover", "Gamer", "Minimalist"]

        # 3. Queue preference request
        req_res = await client.post(
            "/api/v1/preferences/request",
            json={
                "contact_phone": "+919988776655",
                "anonymous": True,
            },
        )
        assert req_res.status_code == 201
        assert req_res.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_mobile_occasion_crud_and_selective_privacy(test_app: FastAPI, test_user: User, async_db: AsyncSession):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create celebration with selective sharing
        create_res = await client.post(
            "/api/v1/occasions",
            json={
                "name": "Aarav Sharma",
                "phone": "+919123456780",
                "relationship": "Best Friend",
                "type": "BIRTHDAY",
                "date": "2026-11-20",
                "recurring": True,
                "is_selective_sharing": True,
                "allowed_friends": ["+919876543210"],  # Current user is allowed
            },
        )
        assert create_res.status_code == 201
        occ = create_res.json()
        occ_id = occ["id"]
        assert occ["name"] == "Aarav Sharma"
        assert occ["is_selective_sharing"] is True

        # Check access list
        access_res = await client.get(f"/api/v1/occasions/{occ_id}/access")
        assert access_res.status_code == 200
        assert access_res.json()["is_selective"] is True
        allowed_phones = [f["phone"] for f in access_res.json()["allowed_friends"]]
        assert "+919876543210" in allowed_phones

        # Add a personal wishlist item for the occasion owner
        item = WishlistItem(
            id=generate_id("wsh"),
            user_id=test_user.id,
            own_occasion_id=occ_id,
            title="Premium Pour-Over Kettle",
            url="https://amazon.in/dp/B000IGOXLS",
            purpose="self",
        )
        async_db.add(item)
        await async_db.commit()

        # Shared wishlist accessible by owner / allowed friend
        shared_res = await client.get(f"/api/v1/occasions/{occ_id}/shared-wishlist")
        assert shared_res.status_code == 200
        items = shared_res.json()
        assert len(items) == 1
        assert items[0]["title"] == "Premium Pour-Over Kettle"


@pytest.mark.asyncio
async def test_shared_wishlist_403_unauthorized_user(test_app: FastAPI, async_db: AsyncSession):
    # Occasion with selective sharing where test_user (+919876543210) is NOT in allowed friends
    occ_id = generate_id("occ")
    occ = Occasion(
        id=occ_id,
        user_id="other_user_celebrant",
        type=OccasionType.BIRTHDAY,
        custom_label="Private Celebration",
        date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        recurring=True,
    )
    async_db.add(occ)
    await async_db.flush()

    # CelebrantOccasion
    cel_occ = CelebrantOccasion(
        id=generate_id("cel"),
        occasion_id=occ_id,
        preferences="[]",
    )
    async_db.add(cel_occ)
    await async_db.flush()

    # Allowed friends does NOT include test_user
    allowed = CelebrantFriend(
        id=generate_id("cfr"),
        celebrant_occasion_id=cel_occ.id,
        phone="+919999999999",
    )
    async_db.add(allowed)
    await async_db.commit()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Accessing private shared wishlist should yield 403 Forbidden
        res = await client.get(f"/api/v1/occasions/{occ_id}/shared-wishlist")
        assert res.status_code == 403
        data = res.json()
        assert data["detail"]["error"] == "access_denied"


@pytest.mark.asyncio
async def test_calendar_sync_and_device_token(test_app: FastAPI):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Calendar sync
        cal_res = await client.post("/api/v1/calendar/google/sync")
        assert cal_res.status_code == 200
        assert "synced_count" in cal_res.json()

        # Device token registration
        token_res = await client.post(
            "/api/v1/notifications/device-token",
            json={
                "token": "fcm_test_token_1234567890_abcdef",
                "platform": "android",
            },
        )
        assert token_res.status_code == 200
        assert token_res.json()["status"] == "ok"

        # In-app notifications listing
        notif_res = await client.get("/api/v1/notifications")
        assert notif_res.status_code == 200
        data = notif_res.json()
        assert isinstance(data, list)
        assert len(data) > 0


@pytest.mark.asyncio
async def test_wishlist_reservation_lifecycle_and_surprise_safe(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    # Celebrant creates occasion and a personal wishlist item
    cel_user_id = generate_id("usr")
    cel_user = User(id=cel_user_id, phone="+919111111111", name="Celebrant User", is_guest=False)
    async_db.add(cel_user)

    occ_id = generate_id("occ")
    occ = Occasion(
        id=occ_id,
        user_id=cel_user_id,
        type=OccasionType.BIRTHDAY,
        custom_label="Celebrant Birthday",
        date=datetime(2026, 12, 1, tzinfo=timezone.utc),
        recurring=True,
    )
    async_db.add(occ)

    item = WishlistItem(
        id=generate_id("wsh"),
        user_id=cel_user_id,
        own_occasion_id=occ_id,
        title="Bialetti Moka Pot 6-Cup",
        url="https://amazon.in/dp/B00004RFRU",
        purpose="self",
        share_status="available",
    )
    async_db.add(item)
    await async_db.commit()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Calling friend reserves item
        res = await client.post(
            "/api/v1/friends/wishlist/reserve",
            json={"item_id": item.id, "occasion_id": occ_id},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "reserved"
        assert "reservation_expires_at" in res.json()

        # 2. Check item status as friend: is_reserved_by_me == True
        friend_view = await client.get(f"/api/v1/occasions/{occ_id}/shared-wishlist")
        assert friend_view.status_code == 200
        f_items = friend_view.json()
        assert len(f_items) == 1
        assert f_items[0]["share_status"] == "reserved"
        assert f_items[0]["is_reserved_by_me"] is True

        # 3. Confirm purchase
        conf_res = await client.post(
            "/api/v1/friends/wishlist/confirm",
            json={"item_id": item.id},
        )
        assert conf_res.status_code == 200
        assert conf_res.json()["status"] == "purchased"

        # 4. Another friend view: item is taken
        post_conf_view = await client.get(f"/api/v1/occasions/{occ_id}/shared-wishlist")
        assert post_conf_view.status_code == 200
        assert post_conf_view.json()[0]["share_status"] == "purchased"
        assert post_conf_view.json()[0]["is_purchased_by_me"] is True


@pytest.mark.asyncio
async def test_wishlist_reservation_with_null_share_status(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    """Regression test: verify item with share_status IS NULL can be reserved."""
    cel_user_id = generate_id("usr")
    cel_user = User(id=cel_user_id, phone="+919222222222", name="Celebrant Null Status", is_guest=False)
    async_db.add(cel_user)

    occ_id = generate_id("occ")
    occ = Occasion(
        id=occ_id,
        user_id=cel_user_id,
        type=OccasionType.BIRTHDAY,
        custom_label="Birthday Celebration",
        date=datetime(2026, 11, 20, tzinfo=timezone.utc),
        recurring=True,
    )
    async_db.add(occ)

    item = WishlistItem(
        id=generate_id("wsh"),
        user_id=cel_user_id,
        own_occasion_id=occ_id,
        title="Null Status Gift",
        url="https://amazon.in/dp/NULLSTATUS01",
        purpose="self",
        share_status=None,  # explicitly NULL in database
    )
    async_db.add(item)
    await async_db.commit()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/friends/wishlist/reserve",
            json={"item_id": item.id, "occasion_id": occ_id},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "reserved"

        await async_db.refresh(item)
        assert item.share_status == "reserved"
        assert item.reserved_by_user_id == test_user.id


@pytest.mark.asyncio
async def test_discovery_and_gift_log_anti_duplicate(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Query discovery feed with vibe filter
        disc_res = await client.post(
            "/api/v1/recommendations/discover",
            json={
                "vibe": "Specialty Coffee",
                "budget_min": 1000,
                "budget_max": 5000,
            },
        )
        assert disc_res.status_code == 200
        disc_data = disc_res.json()
        assert disc_data["count"] > 0
        assert any("Coffee" in item["vibe"] for item in disc_data["items"])
        assert "why_this_fits" in disc_data["items"][0]

        # 2. Create a contact to log past gifts for
        contact = Contact(
            id=generate_id("cnt"),
            user_id=test_user.id,
            name="Rahul Verma",
            phone="+919811122233",
            relationship_type="friend",
        )
        async_db.add(contact)
        await async_db.commit()

        # 3. Log a gift given in 2024
        log_res = await client.post(
            f"/api/v1/contacts/{contact.id}/gifts",
            json={
                "description": "Timemore Chestnut C2 Coffee Grinder",
                "gift_year": "2024",
            },
        )
        assert log_res.status_code == 201
        assert log_res.json()["description"] == "Timemore Chestnut C2 Coffee Grinder"

        # 4. Fetch gifting history timeline
        hist_res = await client.get(f"/api/v1/contacts/{contact.id}/gifts")
        assert hist_res.status_code == 200
        assert len(hist_res.json()) == 1
        assert hist_res.json()[0]["gift_year"] == "2024"

        # 5. Check duplicate warning when proposing another coffee grinder
        dup_res = await client.post(
            "/api/v1/recommendations/check-duplicate",
            json={
                "contact_id": contact.id,
                "product_name": "Timemore Hand Coffee Grinder Pro",
            },
        )
        assert dup_res.status_code == 200
        assert dup_res.json()["is_duplicate"] is True


@pytest.mark.asyncio
async def test_celebrant_surprise_safe_on_wishlist_status(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    """Verify celebrant sees neutral 'on_wishlist' status and never reservation state."""
    occ_id = generate_id("occ")
    occ = Occasion(
        id=occ_id,
        user_id=test_user.id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc),
    )
    async_db.add(occ)
    item = WishlistItem(
        id=generate_id("wsh"),
        user_id=test_user.id,
        url="https://amazon.in/dp/B001",
        title="Kindle Paperwhite",
        purpose="self",
        share_status="reserved",
        reserved_by_user_id="other_friend_user_id",
    )
    async_db.add(item)
    await async_db.commit()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Owner view (test_user owns occasion and wishlist item)
        res = await client.get(f"/api/v1/occasions/{occ_id}/shared-wishlist")
        assert res.status_code == 200
        items = res.json()
        assert len(items) == 1
        # MUST BE NEUTRAL "on_wishlist", NOT "available" and NOT "reserved"
        assert items[0]["share_status"] == "on_wishlist"
        assert items[0]["is_reserved_by_me"] is False
        assert items[0]["is_purchased_by_me"] is False


@pytest.mark.asyncio
async def test_recipient_shortlist_separated_from_personal_wishlist(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    """Verify recipient gift ideas are stored with purpose='gift' and do not pollute personal wishlist."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Add personal item
        p_res = await client.post(
            "/api/v1/wishlist/link",
            json={
                "title": "My Personal AirPods",
                "url": "https://apple.com/airpods",
                "purpose": "self",
            },
        )
        assert p_res.status_code == 201
        assert p_res.json()["purpose"] == "self"

        # 2. Add gift idea for Rohan's birthday
        rohan_occ_id = "occ_rohan_bday"
        g_res = await client.post(
            "/api/v1/wishlist/link",
            json={
                "title": "Gift Idea for Rohan: Coffee Grinder",
                "url": "https://amazon.in/dp/B002",
                "purpose": "gift",
                "recipient_occasion_id": rohan_occ_id,
            },
        )
        assert g_res.status_code == 201
        assert g_res.json()["purpose"] == "gift"

        # 3. Listing default personal wishlist only returns purpose='self'
        my_wishes = await client.get("/api/v1/wishlist")
        assert my_wishes.status_code == 200
        my_titles = [i["title"] for i in my_wishes.json()]
        assert "My Personal AirPods" in my_titles
        assert "Gift Idea for Rohan: Coffee Grinder" not in my_titles

        # 4. Listing recipient shortlist for Rohan returns the gift idea
        rohan_ideas = await client.get(
            f"/api/v1/wishlist?purpose=gift&occasion_id={rohan_occ_id}"
        )
        assert rohan_ideas.status_code == 200
        rohan_titles = [i["title"] for i in rohan_ideas.json()]
        assert "Gift Idea for Rohan: Coffee Grinder" in rohan_titles
        assert "My Personal AirPods" not in rohan_titles


@pytest.mark.asyncio
async def test_device_registration_persistence_multiple_devices(
    test_app: FastAPI, test_user: User, async_db: AsyncSession
):
    """Verify device registration table supports multiple devices and token updates."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register Android device
        res1 = await client.post(
            "/api/v1/notifications/device-token",
            json={"token": "token_pixel_8_pro_12345", "platform": "android"},
        )
        assert res1.status_code == 200

        # Register iPad device
        res2 = await client.post(
            "/api/v1/notifications/device-token",
            json={"token": "token_ipad_air_67890", "platform": "ios"},
        )
        assert res2.status_code == 200

        # Verify both tokens are stored in the database
        stmt = select(DeviceRegistration).where(
            DeviceRegistration.user_id == test_user.id,
            DeviceRegistration.is_active == True,
        )
        regs = list((await async_db.scalars(stmt)).all())
        assert len(regs) == 2
        tokens = {r.token for r in regs}
        assert "token_pixel_8_pro_12345" in tokens
        assert "token_ipad_air_67890" in tokens


@pytest.mark.asyncio
async def test_android_assetlinks_endpoint():
    """Verify /.well-known/assetlinks.json returns valid Digital Asset Links for wishkr."""
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/.well-known/assetlinks.json")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["relation"] == ["delegate_permission/common.handle_all_urls"]
        assert data[0]["target"]["package_name"] == "com.example.wishkr_app"
        assert "EA:4F:41:87:DD:DF:EF:2B:68:15:91:99:A8:A5:62:B4:E9:9D:61:1A:4A:B9:57:1A:05:2B:D7:1E:32:37:79:E1" in data[0]["target"]["sha256_cert_fingerprint_list"]



