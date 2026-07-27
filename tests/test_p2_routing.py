"""P2 deterministic routing regressions (safe exact-intent paths only)."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

os.environ["DATABASE_URL"] = "sqlite:////tmp/wishkr_adk_test.db"
from app.core.config import settings
settings.database_url = "sqlite:////tmp/wishkr_adk_test.db"

from app.bot.agent import handle_message
from app.models import Contact, Occasion, OccasionType, User, WishlistItem
from app.utils.id_gen import generate_id
from datetime import datetime


@pytest.mark.asyncio
async def test_show_my_orders_exact_phrase_gets_mvp_response(db: AsyncSession):
    phone = "+919876543220"
    db.add(User(id=generate_id("usr"), phone=phone, name="OrderUser", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.action_router.list_orders", new=AsyncMock(return_value={"orders": []})) as mock_list_orders:
        with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
            with patch("app.bot.agent._save_message", new=AsyncMock()):
                reply = await handle_message(db, phone, "show my orders")
            mock_get_runner.assert_not_called()
            mock_list_orders.assert_not_called()

    assert "preferred store" in reply


@pytest.mark.asyncio
async def test_payment_status_exact_phrase_gets_mvp_response(db: AsyncSession):
    phone = "+919876543221"
    db.add(User(id=generate_id("usr"), phone=phone, name="PayUser", city_raw="Pune"))
    await db.commit()

    mocked = {"orders": [{"brand": "Amazon", "amount": 1000, "status": "PENDING", "date": "10 May 2026"}]}
    with patch("app.bot.action_router.list_orders", new=AsyncMock(return_value=mocked)) as mock_list_orders:
        with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
            with patch("app.bot.agent._save_message", new=AsyncMock()):
                reply = await handle_message(db, phone, "payment status")
            mock_get_runner.assert_not_called()
            mock_list_orders.assert_not_called()

    assert "preferred store" in reply


@pytest.mark.asyncio
async def test_cart_summary_exact_phrase_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543222"
    db.add(User(id=generate_id("usr"), phone=phone, name="CartUser", city_raw="Pune"))
    await db.commit()

    mocked = {"cart": [{"brand_name": "Amazon", "amount": 500}], "count": 1, "total": 500}
    with patch("app.bot.action_router.view_cart", new=AsyncMock(return_value=mocked)):
        with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
            with patch("app.bot.agent._save_message", new=AsyncMock()):
                reply = await handle_message(db, phone, "cart summary")
            mock_get_runner.assert_not_called()

    assert "cart" in reply.lower() and "500" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "What are all occasions i have told you till now",
        "Can you recall all the occasions whatever i have told you",
        "Show my occasions",
        "List my occasions",
        "What occasions have I added",
        "Which birthdays/anniversaries did I save",
    ],
)
async def test_saved_occasion_phrases_bypass_main_llm_and_list_owned_occasions(
    db: AsyncSession,
    text: str,
):
    phone = "+919876543226"
    user = User(id=generate_id("usr"), phone=phone, name="OccUser", city_raw="Pune")
    contact = Contact(
        id=generate_id("con"),
        user_id=user.id,
        name="Rutuja",
        relationship_type="sibling",
    )
    db.add_all([
        user,
        contact,
        Occasion(
            id=generate_id("occ"),
            user_id=user.id,
            contact_id=contact.id,
            type=OccasionType.BIRTHDAY,
            date=datetime(2026, 5, 5),
            recurring=True,
        ),
    ])
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        reply = await handle_message(db, phone, text)
        mock_get_runner.assert_not_called()

    assert "Here are the occasions I have saved for you:" in reply
    assert "Rutuja" in reply
    assert "sibling" in reply
    assert "Birthday" in reply
    assert "05 May 2027" in reply


@pytest.mark.asyncio
async def test_saved_occasion_empty_state_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543227"
    db.add(User(id=generate_id("usr"), phone=phone, name="NoOccUser", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        reply = await handle_message(db, phone, "show my occasions")
        mock_get_runner.assert_not_called()

    assert "I don't have any occasions saved for you yet" in reply


@pytest.mark.asyncio
async def test_ambiguous_occasion_phrase_does_not_expose_saved_data(db: AsyncSession):
    phone = "+919876543228"
    user = User(id=generate_id("usr"), phone=phone, name="AmbiguousOcc", city_raw="Pune")
    db.add(user)
    db.add(
        Occasion(
            id=generate_id("occ"),
            user_id=user.id,
            contact_id=None,
            type=OccasionType.WEDDING,
            date=datetime(2026, 5, 7),
            recurring=True,
        )
    )
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            await handle_message(db, phone, "what is an occasion")
        assert mock_get_runner.called


@pytest.mark.asyncio
async def test_remove_wishlist_by_url_exact_phrase_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543223"
    user = User(id=generate_id("usr"), phone=phone, name="WishUser", city_raw="Pune")
    db.add(user)
    db.add(
        WishlistItem(
            id=generate_id("wli"),
            user_id=user.id,
            title="Phone",
            url="https://example.com/item",
            purpose="self",
        )
    )
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            reply = await handle_message(db, phone, "remove from wishlist https://example.com/item")
        mock_get_runner.assert_not_called()

    assert "removed" in reply.lower() or "not found" in reply.lower()


@pytest.mark.asyncio
async def test_remove_wishlist_by_item_id_exact_phrase_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543224"
    user = User(id=generate_id("usr"), phone=phone, name="WishUser2", city_raw="Pune")
    item_id = generate_id("wli")
    db.add(user)
    db.add(
        WishlistItem(
            id=item_id,
            user_id=user.id,
            title="Headphones",
            url="https://example.com/hp",
            purpose="self",
        )
    )
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            reply = await handle_message(db, phone, f"delete wishlist item {item_id}")
        mock_get_runner.assert_not_called()

    assert "removed" in reply.lower() or "not found" in reply.lower()


@pytest.mark.asyncio
async def test_ambiguous_orders_message_does_not_deterministically_short_circuit(db: AsyncSession):
    phone = "+919876543225"
    db.add(User(id=generate_id("usr"), phone=phone, name="Ambiguous", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            await handle_message(db, phone, "my order was weird yesterday")
        assert mock_get_runner.called
