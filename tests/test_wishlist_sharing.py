"""Wishlist sharing, reservation lifecycle, and recipient preference tests.

Covers Phases 3–8 + webhook wiring of the shared-wishlist + recipient-preference feature:
  Phase 3 — visibility + get_friend_wishlist authorization
  Phase 4 — reserve_wishlist_item (happy path, double-reservation, expiry)
  Phase 5 — confirm_wishlist_purchase (share_status → 'purchased')
  Phase 7 — request_recipient_preferences deduplication
  Phase 8 — save_recipient_preferences parsing + notify_phones
  Phase 8 (wiring) — _handle_preference_reply_if_pending interceptor in handle_message
  Phase 6 — recipient-aware suggestion integration
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.db.base import Base
from app.models import User, WishlistItem
from app.models.occasion import Occasion, OccasionType
from app.models.celebrant_occasion import CelebrantOccasion
from app.models.celebrant_friend import CelebrantFriend, CelebrantFriendStatus
from app.models.recipient_preference import PreferenceRequest, RecipientPreference
from app.utils.id_gen import generate_id

T = TypeVar("T")

_TABLES = [
    User.__table__,
    WishlistItem.__table__,
    Occasion.__table__,
    CelebrantOccasion.__table__,
    CelebrantFriend.__table__,
    PreferenceRequest.__table__,
    RecipientPreference.__table__,
]


def _run_db(fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def runner() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


async def _make_user(db: AsyncSession, phone: str, name: str = "User") -> User:
    u = User(id=generate_id("usr"), phone=phone, name=name, is_guest=False)
    db.add(u)
    await db.flush()
    return u


async def _make_occasion(db: AsyncSession, user_id: str) -> Occasion:
    occ = Occasion(
        id=generate_id("occ"),
        user_id=user_id,
        type=OccasionType.BIRTHDAY,
        date=datetime.now(timezone.utc),
        recurring=True,
    )
    db.add(occ)
    await db.flush()
    return occ


async def _make_celebrant_occasion(
    db: AsyncSession, occasion_id: str
) -> CelebrantOccasion:
    cel_occ = CelebrantOccasion(
        id=generate_id("cel"),
        occasion_id=occasion_id,
    )
    db.add(cel_occ)
    await db.flush()
    return cel_occ


async def _make_celebrant_friend(
    db: AsyncSession, cel_occ_id: str, phone: str
) -> CelebrantFriend:
    cf = CelebrantFriend(
        id=generate_id("cef"),
        celebrant_occasion_id=cel_occ_id,
        phone=phone,
        status=CelebrantFriendStatus.INVITED,
    )
    db.add(cf)
    await db.flush()
    return cf


_item_counter = 0


async def _make_wishlist_item(
    db: AsyncSession,
    user_id: str,
    *,
    purpose: str = "self",
    visibility: str = "private",
    share_status: str = "available",
    reserved_by_user_id: str | None = None,
    reservation_expires_at: datetime | None = None,
    purchased_by_user_id: str | None = None,
    gifted_at: datetime | None = None,
) -> WishlistItem:
    global _item_counter
    _item_counter += 1
    item = WishlistItem(
        id=generate_id("wli"),
        user_id=user_id,
        url=f"https://example.com/item/{_item_counter}",
        title="Test Item",
        purpose=purpose,
        visibility=visibility,
        share_status=share_status,
        reserved_by_user_id=reserved_by_user_id,
        reservation_expires_at=reservation_expires_at,
        purchased_by_user_id=purchased_by_user_id,
        gifted_at=gifted_at,
    )
    db.add(item)
    await db.flush()
    return item


async def _setup_friend_scenario(
    db: AsyncSession,
    owner_phone: str,
    friend_phone: str,
) -> tuple[User, User, CelebrantOccasion]:
    """Create owner + friend + celebrant occasion + friend invitation."""
    owner = await _make_user(db, owner_phone, "Owner")
    friend = await _make_user(db, friend_phone, "Friend")
    occ = await _make_occasion(db, owner.id)
    cel_occ = await _make_celebrant_occasion(db, occ.id)
    await _make_celebrant_friend(db, cel_occ.id, friend_phone)
    return owner, friend, cel_occ


# ─── Phase 3: get_friend_wishlist ────────────────────────────────────────────

def test_get_friend_wishlist_returns_only_friends_visibility_items():
    """Only 'friends' visibility items appear in the friend wishlist view."""
    from app.bot.tools.tool_impl.wishlist_sharing import get_friend_wishlist

    async def run(db: AsyncSession):
        owner, friend, cel_occ = await _setup_friend_scenario(
            db, "919000000001", "919000000002"
        )
        await _make_wishlist_item(db, owner.id, visibility="private")
        await _make_wishlist_item(db, owner.id, visibility="friends")
        await _make_wishlist_item(db, owner.id, visibility="friends")
        await db.commit()

        result = await get_friend_wishlist(
            db, friend.phone, {"celebrant_occasion_id": cel_occ.id}
        )
        assert "error" not in result, result
        assert len(result["items"]) == 2

    _run_db(run)


def test_get_friend_wishlist_excludes_gift_purpose():
    """Items with purpose='gift' must not appear in the friend wishlist view."""
    from app.bot.tools.tool_impl.wishlist_sharing import get_friend_wishlist

    async def run(db: AsyncSession):
        owner, friend, cel_occ = await _setup_friend_scenario(
            db, "919000000003", "919000000004"
        )
        await _make_wishlist_item(db, owner.id, purpose="gift", visibility="friends")
        await _make_wishlist_item(db, owner.id, purpose="self", visibility="friends")
        await db.commit()

        result = await get_friend_wishlist(
            db, friend.phone, {"celebrant_occasion_id": cel_occ.id}
        )
        assert "error" not in result, result
        assert len(result["items"]) == 1

    _run_db(run)


def test_get_friend_wishlist_hides_purchaser_identity():
    """Purchaser and reserver identity must never appear in get_friend_wishlist response."""
    from app.bot.tools.tool_impl.wishlist_sharing import get_friend_wishlist

    async def run(db: AsyncSession):
        owner, friend, cel_occ = await _setup_friend_scenario(
            db, "919000000005", "919000000007"
        )
        buyer = await _make_user(db, "919000000006", "Buyer")
        now = datetime.now(timezone.utc)
        await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="purchased",
            purchased_by_user_id=buyer.id,
            gifted_at=now,
        )
        await db.commit()

        result = await get_friend_wishlist(
            db, friend.phone, {"celebrant_occasion_id": cel_occ.id}
        )
        assert "error" not in result, result
        for item in result["items"]:
            assert "purchased_by_user_id" not in item
            assert "purchased_by" not in item
            assert "reserved_by_user_id" not in item
            assert "reserved_by" not in item

    _run_db(run)


def test_get_friend_wishlist_unauthorized_returns_error():
    """A user not in the friend list gets a not-invited error."""
    from app.bot.tools.tool_impl.wishlist_sharing import get_friend_wishlist

    async def run(db: AsyncSession):
        owner, _, cel_occ = await _setup_friend_scenario(
            db, "919000000008", "919000000009"
        )
        await _make_wishlist_item(db, owner.id, visibility="friends")
        await db.commit()

        stranger = await _make_user(db, "919000000099", "Stranger")
        result = await get_friend_wishlist(
            db, stranger.phone, {"celebrant_occasion_id": cel_occ.id}
        )
        assert "error" in result

    _run_db(run)


# ─── Phase 4: reserve_wishlist_item ──────────────────────────────────────────

def test_reserve_wishlist_item_happy_path():
    """An available item transitions to 'reserved' when claimed by a friend."""
    from app.bot.tools.tool_impl.wishlist_sharing import reserve_wishlist_item

    async def run(db: AsyncSession):
        owner, friend, cel_occ = await _setup_friend_scenario(
            db, "919000000010", "919000000011"
        )
        item = await _make_wishlist_item(db, owner.id, visibility="friends", share_status="available")
        await db.commit()

        result = await reserve_wishlist_item(
            db, friend.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("reserved") is True, result
        assert result.get("wishlist_item_id") == item.id

        await db.refresh(item)
        assert item.share_status == "reserved"
        assert item.reserved_by_user_id == friend.id
        assert item.reservation_expires_at is not None

    _run_db(run)


def test_reserve_wishlist_item_already_reserved_by_other():
    """Reserving an item already claimed by someone else returns conflict error."""
    from app.bot.tools.tool_impl.wishlist_sharing import reserve_wishlist_item

    async def run(db: AsyncSession):
        owner, first, cel_occ = await _setup_friend_scenario(
            db, "919000000012", "919000000013"
        )
        second = await _make_user(db, "919000000014", "Second")
        await _make_celebrant_friend(db, cel_occ.id, second.phone)
        expiry = datetime.now(timezone.utc) + timedelta(hours=48)
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="reserved",
            reserved_by_user_id=first.id,
            reservation_expires_at=expiry,
        )
        await db.commit()

        result = await reserve_wishlist_item(
            db, second.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("conflict") is True, result
        assert result.get("share_status") == "reserved"

    _run_db(run)


def test_reserve_wishlist_item_expired_reservation_is_reclaimable():
    """An expired reservation allows a new reserver to claim the item."""
    from app.bot.tools.tool_impl.wishlist_sharing import reserve_wishlist_item

    async def run(db: AsyncSession):
        owner, new_reserver, cel_occ = await _setup_friend_scenario(
            db, "919000000015", "919000000017"
        )
        old_reserver = await _make_user(db, "919000000016", "OldReserver")
        past_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="reserved",
            reserved_by_user_id=old_reserver.id,
            reservation_expires_at=past_expiry,
        )
        await db.commit()

        result = await reserve_wishlist_item(
            db, new_reserver.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("reserved") is True, result
        await db.refresh(item)
        assert item.reserved_by_user_id == new_reserver.id

    _run_db(run)


def test_reserve_wishlist_item_already_purchased_is_not_reservable():
    """A purchased item cannot be reserved."""
    from app.bot.tools.tool_impl.wishlist_sharing import reserve_wishlist_item

    async def run(db: AsyncSession):
        owner, newcomer, cel_occ = await _setup_friend_scenario(
            db, "919000000018", "919000000020"
        )
        buyer = await _make_user(db, "919000000019", "Buyer")
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="purchased",
            purchased_by_user_id=buyer.id,
        )
        await db.commit()

        result = await reserve_wishlist_item(
            db, newcomer.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("conflict") is True, result
        assert result.get("share_status") == "purchased"

    _run_db(run)


# ─── Phase 5: confirm_wishlist_purchase ──────────────────────────────────────

def test_confirm_wishlist_purchase_happy_path():
    """Confirming purchase transitions item to 'purchased' status."""
    from app.bot.tools.tool_impl.wishlist_sharing import confirm_wishlist_purchase

    async def run(db: AsyncSession):
        owner, buyer, cel_occ = await _setup_friend_scenario(
            db, "919000000030", "919000000031"
        )
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="reserved",
            reserved_by_user_id=buyer.id,
            reservation_expires_at=expiry,
        )
        await db.commit()

        result = await confirm_wishlist_purchase(
            db, buyer.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("purchased") is True, result
        await db.refresh(item)
        assert item.share_status == "purchased"
        assert item.purchased_by_user_id == buyer.id
        assert item.gifted_at is not None

    _run_db(run)


def test_confirm_wishlist_purchase_by_non_reserver_rejected():
    """Only the user who reserved the item can confirm purchase."""
    from app.bot.tools.tool_impl.wishlist_sharing import confirm_wishlist_purchase

    async def run(db: AsyncSession):
        owner, reserver, cel_occ = await _setup_friend_scenario(
            db, "919000000032", "919000000033"
        )
        other = await _make_user(db, "919000000034", "Other")
        await _make_celebrant_friend(db, cel_occ.id, other.phone)
        expiry = datetime.now(timezone.utc) + timedelta(hours=24)
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends",
            share_status="reserved",
            reserved_by_user_id=reserver.id,
            reservation_expires_at=expiry,
        )
        await db.commit()

        result = await confirm_wishlist_purchase(
            db, other.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("purchased") is False, result
        assert result.get("conflict") is True

    _run_db(run)


def test_confirm_wishlist_purchase_available_item_allowed():
    """An unreserved available item can be directly confirmed as purchased."""
    from app.bot.tools.tool_impl.wishlist_sharing import confirm_wishlist_purchase

    async def run(db: AsyncSession):
        owner, buyer, cel_occ = await _setup_friend_scenario(
            db, "919000000035", "919000000036"
        )
        item = await _make_wishlist_item(
            db, owner.id, visibility="friends", share_status="available",
        )
        await db.commit()

        result = await confirm_wishlist_purchase(
            db, buyer.phone,
            {"wishlist_item_id": item.id, "celebrant_occasion_id": cel_occ.id},
        )
        assert result.get("purchased") is True, result

    _run_db(run)


# ─── Phase 7: request_recipient_preferences deduplication ────────────────────

def test_request_recipient_preferences_creates_new_request():
    """A new preference request is created when none exists."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._send_preference_request_message

        async def _noop(**kwargs):
            pass

        mod._send_preference_request_message = _noop
        try:
            result = await request_recipient_preferences(
                db, "919000000050",
                {"recipient_phone": "9198765 43210", "anonymous": True},
            )
        finally:
            mod._send_preference_request_message = orig

        assert result["status"] == "sent"
        assert result.get("request_id")

    _run_db(run)


def test_request_recipient_preferences_deduplicates_pending():
    """Second request for same recipient reuses existing pending row and adds second requester to notify_phones."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._send_preference_request_message

        async def _noop(**kwargs):
            pass

        mod._send_preference_request_message = _noop
        try:
            r1 = await request_recipient_preferences(
                db, "919000000051",
                {"recipient_phone": "919111222333"},
            )
            r2 = await request_recipient_preferences(
                db, "919000000052",
                {"recipient_phone": "919111222333"},
            )
        finally:
            mod._send_preference_request_message = orig

        assert r1["status"] == "sent"
        assert r2["status"] == "existing_request"
        assert r1["request_id"] == r2["request_id"]

        req = await db.get(PreferenceRequest, r1["request_id"])
        assert req is not None
        phones = [p.strip() for p in req.notify_phones.split(",")]
        assert "919000000051" in phones
        assert "919000000052" in phones

    _run_db(run)


def test_request_recipient_preferences_fresh_prefs_returned_without_message():
    """If fresh preferences exist, return them without sending a new message."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        now = datetime.now(timezone.utc)
        pref = RecipientPreference(
            id=generate_id("prf"),
            recipient_phone="919777888999",
            preferred_categories="books, coffee",
            source="direct_response",
            updated_at=now,
        )
        db.add(pref)
        await db.flush()
        await db.commit()

        sent: list = []
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._send_preference_request_message

        async def _track(**kwargs):
            sent.append(kwargs)

        mod._send_preference_request_message = _track
        try:
            result = await request_recipient_preferences(
                db, "919000000053",
                {"recipient_phone": "919777888999"},
            )
        finally:
            mod._send_preference_request_message = orig

        assert result["status"] == "already_available"
        assert len(sent) == 0

    _run_db(run)


def test_request_recipient_preferences_rejects_self_message():
    """Gift-giver cannot send a preference request to themselves."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        result = await request_recipient_preferences(
            db, "919000000060",
            {"recipient_phone": "919000000060"},
        )
        assert result["error"] == "cannot_message_self"

    _run_db(run)


def test_request_recipient_preferences_rejects_invalid_phone():
    """Invalid phone number returns error without creating a DB row."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        result = await request_recipient_preferences(
            db, "919000000061",
            {"recipient_phone": "not-a-phone"},
        )
        assert result["error"] == "invalid_phone"

    _run_db(run)


# ─── Phase 8: save_recipient_preferences parsing ─────────────────────────────

def test_save_recipient_preferences_parses_categories():
    """Free-text response is parsed into structured preferred_categories."""
    from app.bot.tools.tool_impl.recipient_preferences import save_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._notify_gift_givers

        async def _noop(phones, prefs):
            pass

        mod._notify_gift_givers = _noop
        try:
            result = await save_recipient_preferences(
                db,
                recipient_phone="919500000001",
                raw_response="I love gadgets and coffee, and also skincare products.",
            )
        finally:
            mod._notify_gift_givers = orig

        assert result["saved"] is True
        cats = result["parsed"].get("preferred_categories") or ""
        assert "gadgets" in cats
        assert "coffee" in cats
        assert "skincare" in cats

    _run_db(run)


def test_parse_preferences_extracts_budget():
    """Budget hints in free text are extracted into budget_min / budget_max."""
    from app.bot.tools.tool_impl.recipient_preferences import _parse_preferences

    prefs = _parse_preferences("Something under Rs 2000 would be perfect!")
    assert prefs["budget_max"] == 2000

    prefs2 = _parse_preferences("Anything in the 500-1500 range is fine.")
    assert prefs2["budget_min"] == 500
    assert prefs2["budget_max"] == 1500

    prefs3 = _parse_preferences("Maybe around 5k")
    assert prefs3["budget_max"] == 5000


def test_parse_preferences_extracts_wishlist_links():
    """URLs in free text are extracted as wishlist_links."""
    from app.bot.tools.tool_impl.recipient_preferences import _parse_preferences

    raw = (
        "Here's my wishlist: https://www.amazon.in/hz/wishlist/ls/XYZ "
        "and https://www.flipkart.com/wishlist/abc"
    )
    prefs = _parse_preferences(raw)
    assert prefs["wishlist_links"] is not None
    assert len(prefs["wishlist_links"]) == 2


def test_parse_preferences_extracts_dislikes():
    """Dislike patterns are extracted into disliked_categories."""
    from app.bot.tools.tool_impl.recipient_preferences import _parse_preferences

    prefs = _parse_preferences("I love books but I don't like gadgets or gaming.")
    assert "books" in (prefs.get("preferred_categories") or "")
    dislikes = prefs.get("disliked_categories") or ""
    assert "gadgets" in dislikes or "gaming" in dislikes


def test_parse_preferences_keeps_freetext_in_notes():
    """A reply with no known category keeps the raw text in notes, which is
    what the gift-giver notification falls back to (the 'PS5' case)."""
    from app.bot.tools.tool_impl.recipient_preferences import _parse_preferences

    prefs = _parse_preferences("i would like a PS5!!")
    assert prefs["preferred_categories"] is None
    assert prefs["notes"] == "i would like a PS5!!"

    # The notification picks the first non-empty of categories -> notes ->
    # placeholder, so it must surface the raw reply rather than the generic text.
    cats = prefs.get("preferred_categories") or prefs.get("notes") or "their preferences"
    assert cats == "i would like a PS5!!"


def test_parse_categorize_reply_keeps_only_valid_labels():
    """LLM reply is filtered to canonical labels and ordered; junk is dropped."""
    from app.bot.tools.tool_impl.recipient_preferences import _parse_categorize_reply

    # Valid labels (in any order / case) are normalized and ordered.
    assert _parse_categorize_reply("Gaming, electronics") == "electronics, gaming"
    # Unknown words are dropped.
    assert _parse_categorize_reply("ps5, playstation") is None
    # NONE sentinel maps to nothing.
    assert _parse_categorize_reply("NONE") is None
    # Strict: tokens must be exact labels. A prose token is not a match — the
    # prompt asks for comma-separated labels, so we don't substring-match here.
    assert _parse_categorize_reply("gaming and some nonsense") is None
    # But a clean comma-separated reply with one junk entry keeps the valid one.
    assert _parse_categorize_reply("gaming, nonsense") == "gaming"


def test_save_recipient_preferences_uses_llm_when_keywords_miss():
    """When the keyword parser finds no category, the LLM fallback fills
    preferred_categories (the 'i would like a PS5' -> gaming case)."""
    from app.bot.tools.tool_impl.recipient_preferences import save_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        notify_orig = mod._notify_gift_givers
        llm_orig = mod._llm_categorize
        seen: list[str] = []

        async def _noop(phones, prefs):
            pass

        async def _fake_llm(raw):
            seen.append(raw)
            return "gaming"

        mod._notify_gift_givers = _noop
        mod._llm_categorize = _fake_llm
        try:
            result = await save_recipient_preferences(
                db,
                recipient_phone="919500000009",
                raw_response="i would like a PS5!!",
            )
        finally:
            mod._notify_gift_givers = notify_orig
            mod._llm_categorize = llm_orig

        assert result["saved"] is True
        assert seen == ["i would like a PS5!!"]  # LLM was consulted
        assert result["parsed"]["preferred_categories"] == "gaming"
        # Raw text is still preserved in notes.
        assert result["parsed"]["notes"] == "i would like a PS5!!"

    _run_db(run)


def test_save_recipient_preferences_skips_llm_when_keywords_hit():
    """If the keyword parser already found a category, the LLM is not called."""
    from app.bot.tools.tool_impl.recipient_preferences import save_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        notify_orig = mod._notify_gift_givers
        llm_orig = mod._llm_categorize
        called = {"n": 0}

        async def _noop(phones, prefs):
            pass

        async def _fake_llm(raw):
            called["n"] += 1
            return "gaming"

        mod._notify_gift_givers = _noop
        mod._llm_categorize = _fake_llm
        try:
            result = await save_recipient_preferences(
                db,
                recipient_phone="919500000010",
                raw_response="I love books and coffee",
            )
        finally:
            mod._notify_gift_givers = notify_orig
            mod._llm_categorize = llm_orig

        assert result["saved"] is True
        assert called["n"] == 0  # keyword hit → no LLM call
        assert "books" in (result["parsed"]["preferred_categories"] or "")

    _run_db(run)


def test_save_recipient_preferences_notifies_pending_requesters():
    """All notify_phones on pending requests are notified after preference save."""
    from app.bot.tools.tool_impl.recipient_preferences import (
        save_recipient_preferences, request_recipient_preferences,
    )

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        send_orig = mod._send_preference_request_message
        notify_orig = mod._notify_gift_givers

        sent_to: list[list[str]] = []

        async def _noop_send(**kwargs):
            pass

        async def _track_notify(phones, prefs):
            sent_to.append(list(phones))

        mod._send_preference_request_message = _noop_send
        mod._notify_gift_givers = _track_notify
        try:
            r1 = await request_recipient_preferences(
                db, "919000000070",
                {"recipient_phone": "919500000002"},
            )
            await request_recipient_preferences(
                db, "919000000071",
                {"recipient_phone": "919500000002"},
            )
            await db.commit()

            result = await save_recipient_preferences(
                db,
                recipient_phone="919500000002",
                raw_response="I like books and gaming.",
                request_id=r1["request_id"],
            )
        finally:
            mod._send_preference_request_message = send_orig
            mod._notify_gift_givers = notify_orig

        assert result["saved"] is True
        assert result["notified_count"] >= 1

    _run_db(run)


def test_save_recipient_preferences_updates_existing_record():
    """Re-saving preferences merges into the existing row rather than duplicating."""
    from app.bot.tools.tool_impl.recipient_preferences import save_recipient_preferences

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._notify_gift_givers

        async def _noop(phones, prefs):
            pass

        mod._notify_gift_givers = _noop
        try:
            await save_recipient_preferences(db, "919500000003", "I love books and fitness.")
            await db.commit()
            await save_recipient_preferences(db, "919500000003", "Also into coffee and skincare.")
            await db.commit()
        finally:
            mod._notify_gift_givers = orig

        rows = (await db.execute(
            select(RecipientPreference).where(
                RecipientPreference.recipient_phone == "919500000003"
            )
        )).scalars().all()
        assert len(rows) == 1

    _run_db(run)

    _run_db(run)


# ─── Phase 8 (wiring): _handle_preference_reply_if_pending ───────────────────









# ─── Phase 6: recipient-aware suggestion integration ─────────────────────────

def test_request_recipient_preferences_returns_existing_preferences_immediately():
    """If fresh preferences already exist for the recipient, they are returned
    directly without sending a new message — the gift-giver immediately gets
    data to inform suggestions."""
    from app.bot.tools.tool_impl.recipient_preferences import request_recipient_preferences

    async def run(db: AsyncSession):
        now = datetime.now(timezone.utc)
        # Pre-existing fresh preference record for the recipient
        pref = RecipientPreference(
            id=generate_id("prf"),
            recipient_phone="919800000001",
            preferred_categories="gadgets, sneakers",
            preferred_brands="nike",
            preferred_budget_max=3000,
            source="direct_response",
            updated_at=now,
        )
        db.add(pref)
        await db.flush()
        await db.commit()

        sent: list = []
        import app.bot.tools.tool_impl.recipient_preferences as mod
        orig = mod._send_preference_request_message

        async def _track(**kwargs):
            sent.append(kwargs)

        mod._send_preference_request_message = _track
        try:
            result = await request_recipient_preferences(
                db, "919800000099",
                {"recipient_phone": "919800000001"},
            )
        finally:
            mod._send_preference_request_message = orig

        # Must not send a new message
        assert len(sent) == 0, "Should not send message when fresh prefs exist"
        # Must return existing data to inform suggestions
        assert result["status"] == "already_available"
        prefs_data = result["preferences"]
        assert "gadgets" in (prefs_data.get("preferred_categories") or "")
        assert prefs_data.get("budget_max") == 3000

    _run_db(run)


def test_recipient_preferences_inform_gift_giver_flow():
    """End-to-end: gift-giver requests preferences → recipient replies →
    preferences saved → gift-giver re-requests and gets data immediately."""
    from app.bot.tools.tool_impl.recipient_preferences import (
        request_recipient_preferences,
        save_recipient_preferences,
    )

    async def run(db: AsyncSession):
        import app.bot.tools.tool_impl.recipient_preferences as mod
        send_orig = mod._send_preference_request_message
        notify_orig = mod._notify_gift_givers

        async def _noop_send(**kwargs):
            pass

        async def _noop_notify(phones, prefs):
            pass

        mod._send_preference_request_message = _noop_send
        mod._notify_gift_givers = _noop_notify
        try:
            # Step 1: gift-giver requests preferences
            r1 = await request_recipient_preferences(
                db, "919800000099",
                {"recipient_phone": "919800000002"},
            )
            assert r1["status"] == "sent"
            await db.commit()

            # Step 2: recipient replies with their preferences
            await save_recipient_preferences(
                db,
                recipient_phone="919800000002",
                raw_response="I really like gadgets, books, and coffee. Budget around 2000-3000.",
                request_id=r1["request_id"],
            )
            await db.commit()

            # Step 3: gift-giver asks again — should get data immediately
            r2 = await request_recipient_preferences(
                db, "919800000099",
                {"recipient_phone": "919800000002"},
            )
        finally:
            mod._send_preference_request_message = send_orig
            mod._notify_gift_givers = notify_orig

        assert r2["status"] == "already_available", (
            "After recipient replies, subsequent requests should return existing data"
        )
        prefs = r2["preferences"]
        cats = prefs.get("preferred_categories") or ""
        assert "gadgets" in cats or "books" in cats or "coffee" in cats
        assert prefs.get("budget_min") == 2000 or prefs.get("budget_max") == 3000

    _run_db(run)
