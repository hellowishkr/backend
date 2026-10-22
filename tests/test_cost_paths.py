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
from app.models import User
from app.utils.id_gen import generate_id








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
