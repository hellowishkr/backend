from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import google.adk.sessions as _adk_sessions
import pytest
from sqlalchemy.ext.asyncio import AsyncSession


class _StubSessionService:
    def __init__(self, **_kw):
        self._session = SimpleNamespace(
            id="test_session",
            events=[],
            last_update_time=datetime.now(timezone.utc).timestamp(),
        )

    async def list_sessions(self, **_kw):
        return SimpleNamespace(sessions=[self._session])

    async def create_session(self, **_kw):
        return self._session

    async def get_session(self, **_kw):
        return self._session

    async def delete_session(self, **_kw):
        return None


_adk_sessions.DatabaseSessionService = _StubSessionService  # type: ignore[assignment]

from app.bot import agent
from app.bot.state_utils import (
    active_gift_context_is_recent,
    clear_active_gift_context,
    get_active_gift_context,
    get_state,
    merge_active_gift_context,
    update_active_gift_context,
)
from app.bot.tools.tool_impl import occasions
from app.models import User


async def _make_user(db: AsyncSession, phone: str = "919000000001") -> User:
    user = User(id="usr_active_ctx", phone=phone, name="Karan", city_raw="Pune")
    db.add(user)
    await db.flush()
    return user


async def _add_swaraj_birthday(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    phone: str = "919000000001",
) -> dict:
    monkeypatch.setattr(
        occasions,
        "schedule_reminders",
        AsyncMock(return_value={
            "reminders_created": 0,
            "days_until": 0,
            "tight_window": False,
        }),
    )
    return await occasions.add_occasion(
        db,
        phone,
        {
            "contact_name": "Swaraj",
            "contact_phone": "9359615424",
            "occasion_type": "BIRTHDAY",
            "date_day": datetime.now(timezone.utc).day,
            "date_month": datetime.now(timezone.utc).month,
            "relationship": "friend",
        },
    )


@pytest.mark.asyncio
async def test_active_gift_context_created_when_friend_birthday_added(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "919000000001"
    await _make_user(db, phone)

    result = await _add_swaraj_birthday(db, monkeypatch, phone)
    state = await get_state(db, phone)
    ctx = get_active_gift_context(state)

    assert result["success"] is True
    assert ctx is not None
    assert ctx["recipient_name"] == "Swaraj"
    assert ctx["recipient_phone"] == "919359615424"
    assert ctx["occasion_id"] == result["occasion_id"]
    assert ctx["occasion_type"] == "Birthday"
    assert ctx["purpose"] == "gift_for_friend"
    assert ctx["current_step"] == "occasion_saved"


@pytest.mark.asyncio
async def test_active_gift_context_adds_interest_bgmi(db: AsyncSession) -> None:
    phone = "919000000002"
    await update_active_gift_context(
        db,
        phone,
        {"recipient_name": "Swaraj", "purpose": "gift_for_friend"},
    )

    await update_active_gift_context(
        db,
        phone,
        agent._active_gift_context_updates_from_text("He likes BGMI and mobile games"),
    )
    ctx = get_active_gift_context(await get_state(db, phone))

    assert ctx is not None
    assert "BGMI" in ctx["interest"]
    assert "mobile gaming" in ctx["recipient_vibe"]
    assert ctx["current_step"] == "interest_known"


@pytest.mark.asyncio
async def test_active_gift_context_adds_headphones_category(db: AsyncSession) -> None:
    phone = "919000000003"
    await update_active_gift_context(
        db,
        phone,
        {"recipient_name": "Swaraj", "purpose": "gift_for_friend"},
    )

    await update_active_gift_context(
        db,
        phone,
        agent._active_gift_context_updates_from_text("I'll go for headphones"),
    )
    ctx = get_active_gift_context(await get_state(db, phone))

    assert ctx is not None
    assert ctx["gift_category"] == "headphones"
    assert ctx["current_step"] == "category_selected"


@pytest.mark.asyncio
async def test_active_gift_context_keeps_clothing_and_art_interests(
    db: AsyncSession,
) -> None:
    phone = "919000000033"
    await update_active_gift_context(
        db,
        phone,
        {"recipient_name": "Vidushi", "purpose": "gift_for_friend"},
    )

    await update_active_gift_context(
        db,
        phone,
        agent._active_gift_context_updates_from_text(
            "Some comfy clothes, art supplies"
        ),
    )
    ctx = get_active_gift_context(await get_state(db, phone))

    assert ctx is not None
    assert "comfy clothes" in ctx["recipient_vibe"]
    assert "art supplies" in ctx["recipient_vibe"]
    assert ctx["current_step"] == "interest_known"


@pytest.mark.asyncio
async def test_active_gift_context_dynamic_tail_mentions_recipient_interest_category(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "919000000004"
    user = await _make_user(db, phone)
    await _add_swaraj_birthday(db, monkeypatch, phone)
    await update_active_gift_context(
        db,
        phone,
        {
            "interest": "BGMI/mobile gaming",
            "recipient_vibe": "mobile gaming",
            "gift_category": "headphones",
        },
    )

    tail = await agent._build_dynamic_tail(db, phone, user, None)

    assert "Active gift context:" in tail
    assert "Swaraj's Birthday" in tail
    assert "Known interest: BGMI/mobile gaming." in tail
    assert "Current gift direction: headphones." in tail
    assert "Do not ask who the gift is for" in tail


def test_active_gift_context_does_not_overwrite_recipient_on_followup() -> None:
    existing = {
        "recipient_name": "Swaraj",
        "occasion_id": "occ_swaraj",
        "purpose": "gift_for_friend",
    }

    merged = merge_active_gift_context(
        existing,
        {"recipient_name": "Rahul", "gift_category": "headphones"},
    )

    assert merged["recipient_name"] == "Swaraj"
    assert merged["occasion_id"] == "occ_swaraj"
    assert merged["gift_category"] == "headphones"


def test_active_gift_context_expires_or_clears_when_appropriate() -> None:
    now = datetime.now(timezone.utc)
    old_ctx = {
        "recipient_name": "Swaraj",
        "updated_at": (now - timedelta(hours=25)).isoformat(),
    }
    fresh_ctx = {
        "recipient_name": "Swaraj",
        "updated_at": (now - timedelta(hours=1)).isoformat(),
    }

    assert active_gift_context_is_recent(old_ctx, now=now) is False
    assert active_gift_context_is_recent(fresh_ctx, now=now) is True
    assert clear_active_gift_context({"active_gift_context": fresh_ctx}) == {}


@pytest.mark.asyncio
async def test_swaraj_active_gift_context_sequence(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "919000000005"
    await _make_user(db, phone)
    await _add_swaraj_birthday(db, monkeypatch, phone)

    await update_active_gift_context(
        db,
        phone,
        agent._active_gift_context_updates_from_text("He likes BGMI"),
    )
    await update_active_gift_context(
        db,
        phone,
        agent._active_gift_context_updates_from_text("I'll go for headphones"),
    )

    ctx = get_active_gift_context(await get_state(db, phone))
    assert ctx is not None
    assert ctx["recipient_name"] == "Swaraj"
    assert ctx["purpose"] == "gift_for_friend"
    assert "BGMI" in ctx["interest"]
    assert "mobile gaming" in ctx["recipient_vibe"]
    assert ctx["gift_category"] == "headphones"
