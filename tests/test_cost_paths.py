"""Cost-path regression tests for deterministic bypass behavior."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Prevent ADK session service from attempting a real Postgres connection in tests.
os.environ["DATABASE_URL"] = "sqlite:////tmp/wishkr_adk_test.db"
from app.core.config import settings
settings.database_url = "sqlite:////tmp/wishkr_adk_test.db"

from app.bot.agent import handle_message
from app.bot.intent_router import RouteResult
from app.models import User, WishlistItem
from app.models.recipient_preference import PreferenceRequest
from app.utils.id_gen import generate_id


@pytest.mark.asyncio
async def test_show_my_wishlist_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543210"
    user = User(id=generate_id("usr"), phone=phone, name="TestUser", city_raw="Pune")
    db.add(user)
    db.add(
        WishlistItem(
            id=generate_id("wli"),
            user_id=user.id,
            title="Test Product",
            url="https://example.com/product",
            purpose="self",
        )
    )
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            reply = await handle_message(db, phone, "show my wishlist")
        mock_get_runner.assert_not_called()

    assert "wishlist" in reply.lower() or "test product" in reply.lower()


@pytest.mark.asyncio
async def test_menu_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543211"
    db.add(User(id=generate_id("usr"), phone=phone, name="MenuUser", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
        with patch("app.bot.agent._save_message", new=AsyncMock()):
            reply = await handle_message(db, phone, "menu")
        mock_get_runner.assert_not_called()

    assert "menu" in reply.lower() or "occasion" in reply.lower() or "order" in reply.lower()


@pytest.mark.asyncio
async def test_preference_reply_bypasses_main_llm(db: AsyncSession):
    phone = "+919876543212"
    db.add(
        PreferenceRequest(
            id=generate_id("preq"),
            recipient_phone=phone,
            requester_phone="+919000000000",
            status="pending",
        )
    )
    await db.commit()

    with patch("app.bot.agent.settings.preference_interceptor_enabled", True):
        with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
            with patch("app.bot.agent._save_message", new=AsyncMock()):
                reply = await handle_message(db, phone, "I like gadgets and watches")
            mock_get_runner.assert_not_called()

    assert "thanks" in reply.lower() or "shared" in reply.lower()


@pytest.mark.asyncio
async def test_budget_blocked_does_not_call_gemini(db: AsyncSession):
    phone = "+919876543213"
    db.add(User(id=generate_id("usr"), phone=phone, name="BudgetUser", city_raw="Pune"))
    await db.commit()

    with patch("app.bot.agent.check_all_budgets", new=AsyncMock(return_value=(False, "global_daily"))):
        with patch("app.bot.agent._get_runner_for_phase") as mock_get_runner:
            mock_runner = AsyncMock()
            mock_get_runner.return_value = mock_runner
            with patch(
                "app.bot.agent.route_intent",
                new=AsyncMock(return_value=RouteResult(intent="other", confidence=1.0, source="llm")),
            ):
                with patch("app.bot.agent._save_message", new=AsyncMock()):
                    reply = await handle_message(db, phone, "suggest gifts for me")
                mock_runner.run_async.assert_not_called()

    assert reply
