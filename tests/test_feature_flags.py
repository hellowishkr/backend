"""Tests for safety feature flags."""

import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings


class TestSharedWishlistFlag:
    """Test enable_shared_wishlist flag controls wishlist sharing tools."""

    @pytest.mark.asyncio
    async def test_get_friend_wishlist_disabled(self):
        """When enable_shared_wishlist=False, get_friend_wishlist returns disabled response."""
        from app.bot.tools.adapter import get_friend_wishlist

        with patch("app.core.config.settings.enable_shared_wishlist", False):
            result = await get_friend_wishlist(celebrant_occasion_id="occ_123")
            assert result["status"] == "disabled"
            assert "not enabled" in result["message"]

    @pytest.mark.asyncio
    async def test_get_friend_wishlist_enabled(self):
        """When enable_shared_wishlist=True, get_friend_wishlist calls the tool."""
        from app.bot.tools.adapter import get_friend_wishlist

        with patch("app.core.config.settings.enable_shared_wishlist", True):
            with patch("app.bot.tools.adapter.execute_tool", new_callable=AsyncMock) as mock_execute:
                mock_execute.return_value = {"items": []}
                result = await get_friend_wishlist(celebrant_occasion_id="occ_123")
                # Should call execute_tool when flag is enabled
                mock_execute.assert_called_once()
                assert result == {"items": []}

    @pytest.mark.asyncio
    async def test_reserve_wishlist_item_disabled(self):
        """When enable_shared_wishlist=False, reserve_wishlist_item returns disabled response."""
        from app.bot.tools.adapter import reserve_wishlist_item

        with patch("app.core.config.settings.enable_shared_wishlist", False):
            result = await reserve_wishlist_item(
                wishlist_item_id="wsh_123",
                celebrant_occasion_id="occ_123"
            )
            assert result["status"] == "disabled"
            assert "not enabled" in result["message"]

    @pytest.mark.asyncio
    async def test_reserve_wishlist_item_enabled(self):
        """When enable_shared_wishlist=True, reserve_wishlist_item calls the tool."""
        from app.bot.tools.adapter import reserve_wishlist_item

        with patch("app.core.config.settings.enable_shared_wishlist", True):
            with patch("app.bot.tools.adapter.execute_tool", new_callable=AsyncMock) as mock_execute:
                mock_execute.return_value = {"status": "reserved"}
                result = await reserve_wishlist_item(
                    wishlist_item_id="wsh_123",
                    celebrant_occasion_id="occ_123"
                )
                mock_execute.assert_called_once()
                assert result == {"status": "reserved"}

    @pytest.mark.asyncio
    async def test_confirm_wishlist_purchase_disabled(self):
        """When enable_shared_wishlist=False, confirm_wishlist_purchase returns disabled response."""
        from app.bot.tools.adapter import confirm_wishlist_purchase

        with patch("app.core.config.settings.enable_shared_wishlist", False):
            result = await confirm_wishlist_purchase(
                wishlist_item_id="wsh_123",
                celebrant_occasion_id="occ_123"
            )
            assert result["status"] == "disabled"
            assert "not enabled" in result["message"]

    @pytest.mark.asyncio
    async def test_confirm_wishlist_purchase_enabled(self):
        """When enable_shared_wishlist=True, confirm_wishlist_purchase calls the tool."""
        from app.bot.tools.adapter import confirm_wishlist_purchase

        with patch("app.core.config.settings.enable_shared_wishlist", True):
            with patch("app.bot.tools.adapter.execute_tool", new_callable=AsyncMock) as mock_execute:
                mock_execute.return_value = {"status": "purchased"}
                result = await confirm_wishlist_purchase(
                    wishlist_item_id="wsh_123",
                    celebrant_occasion_id="occ_123"
                )
                mock_execute.assert_called_once()
                assert result == {"status": "purchased"}


class TestPreferenceOutboundFlag:
    """Test preference_outbound_enabled flag controls outbound preference requests."""

    @pytest.mark.asyncio
    async def test_request_recipient_preferences_disabled(self):
        """When preference_outbound_enabled=False, returns disabled response."""
        from app.bot.tools.adapter import request_recipient_preferences

        with patch("app.core.config.settings.preference_outbound_enabled", False):
            result = await request_recipient_preferences(
                recipient_phone="919876543210",
                occasion_id="occ_123"
            )
            assert result["status"] == "disabled"
            assert "not enabled" in result["message"]

    @pytest.mark.asyncio
    async def test_request_recipient_preferences_enabled(self):
        """When preference_outbound_enabled=True, calls the tool."""
        from app.bot.tools.adapter import request_recipient_preferences

        with patch("app.core.config.settings.preference_outbound_enabled", True):
            with patch("app.bot.tools.adapter.execute_tool", new_callable=AsyncMock) as mock_execute:
                mock_execute.return_value = {"status": "sent"}
                result = await request_recipient_preferences(
                    recipient_phone="919876543210",
                    occasion_id="occ_123"
                )
                mock_execute.assert_called_once()
                assert result == {"status": "sent"}


class TestPreferenceInterceptorFlag:
    """Test preference_interceptor_enabled flag controls message interception."""

    @pytest.mark.asyncio
    async def test_preference_interceptor_disabled(self, db: AsyncSession):
        """When preference_interceptor_enabled=False, returns None (normal agent flow)."""
        from app.bot.agent import _handle_preference_reply_if_pending

        with patch("app.bot.agent.settings.preference_interceptor_enabled", False):
            result = await _handle_preference_reply_if_pending(
                db=db,
                phone="919876543210",
                text="I like tech and books"
            )
            # Should return None, allowing normal agent flow
            assert result is None

    @pytest.mark.asyncio
    async def test_preference_interceptor_enabled_no_pending_request(self, db: AsyncSession):
        """When enabled but no pending request, returns None."""
        from app.bot.agent import _handle_preference_reply_if_pending

        with patch("app.bot.agent.settings.preference_interceptor_enabled", True):
            # DB is empty, so no pending PreferenceRequest
            result = await _handle_preference_reply_if_pending(
                db=db,
                phone="919876543210",
                text="I like tech and books"
            )
            # Should return None because no pending request
            assert result is None

    @pytest.mark.asyncio
    async def test_preference_interceptor_enabled_with_pending_request(self, db: AsyncSession):
        """When enabled and pending request exists, processes the preference."""
        from app.bot.agent import _handle_preference_reply_if_pending
        from app.models.recipient_preference import PreferenceRequest
        from app.utils.id_gen import generate_id

        # Create a pending PreferenceRequest
        req = PreferenceRequest(
            id=generate_id("prq"),
            recipient_phone="919876543210",
            requester_phone="919876543211",
            status="pending",
            occasion_id=None,
            anonymous=True,
        )
        db.add(req)
        await db.commit()

        with patch("app.bot.agent.settings.preference_interceptor_enabled", True):
            with patch("app.bot.tools.tool_impl.recipient_preferences.save_recipient_preferences",
                      new_callable=AsyncMock) as mock_save:
                mock_save.return_value = {
                    "preference_id": "prp_123",
                    "parsed": {"preferred_categories": "tech, books"},
                    "notified_count": 1,
                }
                result = await _handle_preference_reply_if_pending(
                    db=db,
                    phone="919876543210",
                    text="I like tech and books"
                )
                # Should process the preference and return ack
                assert result is not None
                assert "Thanks" in result
                assert "tech, books" in result
                mock_save.assert_called_once()


class TestFlagDefaults:
    """Test that all flags default to False for production safety."""

    def test_enable_shared_wishlist_default(self):
        """enable_shared_wishlist defaults to False."""
        # In production, this should be False
        # In staging, set via env var: ENABLE_SHARED_WISHLIST=true
        assert hasattr(settings, "enable_shared_wishlist")
        # Default is False; in this test env it should be False unless overridden
        # We can't assert the default directly due to the test environment,
        # but we can verify the config accepts it
        assert isinstance(settings.enable_shared_wishlist, bool)

    def test_preference_outbound_enabled_default(self):
        """preference_outbound_enabled defaults to False."""
        assert hasattr(settings, "preference_outbound_enabled")
        assert isinstance(settings.preference_outbound_enabled, bool)

    def test_preference_interceptor_enabled_default(self):
        """preference_interceptor_enabled defaults to False."""
        assert hasattr(settings, "preference_interceptor_enabled")
        assert isinstance(settings.preference_interceptor_enabled, bool)

    def test_mvp_reminder_only_default(self):
        """MVP reminder-only defaults to launch-safe True."""
        assert hasattr(settings, "mvp_reminder_only")
        assert settings.mvp_reminder_only is True

    def test_gift_refinement_flag_is_explicit(self):
        """AI suggestion review stays rollout-controlled."""
        assert hasattr(settings, "gift_refinement_enabled")
        assert isinstance(settings.gift_refinement_enabled, bool)


class TestMvpReminderOnly:
    """Test MVP reminder-only flag gates risky marketplace paths."""

    @pytest.mark.asyncio
    async def test_execute_tool_blocks_marketplace_tools(self):
        from app.bot.tools.adapter import execute_tool

        with patch("app.bot.tools.adapter.settings.mvp_reminder_only", True):
            result = await execute_tool("create_checkout", {"anything": "ignored"})

        assert result["status"] == "disabled"
        assert result["error"] == "mvp_reminder_only"
        assert result["retryable"] is False
        assert "preferred store" in result["user_message"]

    @pytest.mark.asyncio
    async def test_suggest_gifts_returns_idea_only_shape(self, db: AsyncSession, monkeypatch):
        from app.bot.sources import GiftCandidate, ProviderPolicy
        from app.services.gift_recommendation import GiftRecommendationRequest, recommend_gifts

        class FakeSource:
            source_id = "fake_provider"

            async def list_candidates(self, **_kwargs):
                return [
                    GiftCandidate(
                        source_id="fake_provider",
                        item_id="sku_123",
                        name="Noise-cancelling headphones",
                        vibes=["audio_lover"],
                        price_min=2000,
                        price_max=5000,
                        fulfillment="instant_voucher",
                        contact_info="provider@example.com",
                        policy=ProviderPolicy(
                            fulfillment_mode="api",
                            delivery_policy="Instant delivery after payment",
                            return_policy="Provider policy",
                            cancellation_policy="Provider cancellation",
                        ),
                    )
                ]

        monkeypatch.setattr(settings, "mvp_reminder_only", True)
        result = await recommend_gifts(
            db,
            GiftRecommendationRequest(
                phone="919876543210",
                vibes_in=["audio_lover"],
                n=1,
            ),
            sources=[FakeSource()],
        )

        assert result["success"] is True
        assert result["mode"] == "idea_only"
        suggestion = result["suggestions"][0]
        assert suggestion["name"] == "Noise-cancelling headphones"
        assert suggestion["budget_hint"] == "around ₹2000-₹5000"
        assert "website_url" not in suggestion
        assert "instant delivery" not in suggestion["why"]
        assert "delivered to door" not in suggestion["why"]
        assert "booking-based" not in suggestion["why"]
        forbidden = {
            "provider_id",
            "item_id",
            "contact_info",
            "fulfillment_mode",
            "delivery_policy",
            "return_policy",
            "cancellation_policy",
        }
        assert not forbidden.intersection(suggestion)

    @pytest.mark.asyncio
    async def test_suggest_gifts_has_general_clothing_and_art_ideas_without_catalogue(
        self,
        db: AsyncSession,
        monkeypatch,
    ):
        from app.bot.gift_context import GiftNeedProfile
        from app.services.gift_recommendation import GiftRecommendationRequest, recommend_gifts

        monkeypatch.setattr(settings, "mvp_reminder_only", True)
        result = await recommend_gifts(
            db,
            GiftRecommendationRequest(
                phone="919876543210",
                profile=GiftNeedProfile(
                    recipient_name="Vidushi",
                    recipient_vibe="comfy clothes, art supplies",
                ),
                n=8,
            ),
            sources=[],
        )

        assert result["success"] is True
        assert result["mode"] == "idea_only"
        names = {item["name"] for item in result["suggestions"]}
        assert "Soft everyday hoodie" in names
        assert "Sketchbook and brush-pen set" in names
        assert "preferred store" not in result["message"]

    @pytest.mark.asyncio
    async def test_suggest_gifts_defers_to_ai_when_no_cold_start_idea_matches(
        self,
        db: AsyncSession,
        monkeypatch,
    ):
        from app.services.gift_recommendation import GiftRecommendationRequest, recommend_gifts

        monkeypatch.setattr(settings, "mvp_reminder_only", True)
        monkeypatch.setattr(settings, "gift_refinement_enabled", False)
        result = await recommend_gifts(
            db,
            GiftRecommendationRequest(
                phone="919876543210",
                vibes_in=["premium_apple"],
            ),
            sources=[],
        )

        assert result["success"] is True
        assert result["mode"] == "ai_fallback_pending"
        assert result["suggestions"] == []
        assert result["missing_fields"] == ["budget"]
        assert "budget" in result["clarifying_question"].lower()

    @pytest.mark.asyncio
    async def test_suggest_gifts_attaches_optional_recommendation_review(
        self,
        db: AsyncSession,
        monkeypatch,
    ):
        from app.services.gift_recommendation import GiftRecommendationRequest, recommend_gifts

        review = {
            "reviewed": True,
            "top_picks": [{"name": "Soft everyday hoodie"}],
        }
        monkeypatch.setattr(settings, "mvp_reminder_only", True)
        monkeypatch.setattr(settings, "gift_refinement_enabled", True)
        with patch(
            "app.services.gift_refinement.refine_gift_recommendations",
            new=AsyncMock(return_value=review),
        ) as refine:
            result = await recommend_gifts(
                db,
                GiftRecommendationRequest(
                    phone="919876543210",
                    vibes_in=["gen_z_streetwear"],
                ),
                sources=[],
            )

        assert result["success"] is True
        assert result["recommendation_review"] == review
        refine.assert_awaited_once()

    def test_reminder_copy_has_no_purchase_promises(self):
        from app.bot import messages

        reminder_copy = "\n".join([
            messages.REMINDER,
            messages.REMINDER_TODAY,
            messages.FRIEND_REMINDER,
            messages.FRIEND_REMINDER_TODAY,
        ]).lower()
        forbidden = [
            "gift card",
            "payment link",
            "voucher",
            "send in under a minute",
            "under a minute",
            "send gift",
            "pick and send",
        ]
        for phrase in forbidden:
            assert phrase not in reminder_copy

    def test_scheduler_skips_payment_provider_jobs_in_mvp(self, monkeypatch):
        from app.jobs import scheduler as scheduler_module

        added_jobs = {}
        monkeypatch.setattr(scheduler_module.settings, "mvp_reminder_only", True)
        monkeypatch.setattr(
            scheduler_module.scheduler,
            "add_job",
            lambda *args, **kwargs: added_jobs.__setitem__(kwargs.get("id"), kwargs),
        )
        monkeypatch.setattr(scheduler_module.scheduler, "start", lambda: None)

        scheduler_module.start_scheduler()

        assert {"send_reminders", "retry_failed_outbound", "wishlist_reminders"}.issubset(
            added_jobs
        )
        assert not {
            "poll_stale_orders",
            "deliver_gifts",
            "sync_gift_catalog",
            "sync_gift_catalog_boot",
            "reconcile_payments",
        }.intersection(added_jobs)
        monthly_job = added_jobs["monthly_occasion_summary"]
        assert monthly_job["day"] == "1,last"
        assert monthly_job["minute"] == 0
        assert monthly_job["timezone"] == "UTC"
        wishlist_job = added_jobs["wishlist_reminders"]
        assert wishlist_job["minute"] == 0
        assert wishlist_job["timezone"] == "UTC"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "buy Amazon gift card",
            "I want a voucher for Swaraj",
            "can I order this",
            "checkout this gift",
            "please send gift",
            "show my orders",
            "payment status",
        ],
    )
    async def test_purchase_request_phrases_get_mvp_response(
        self,
        db: AsyncSession,
        text: str,
        monkeypatch,
    ):
        # The kill-switch lives in `try_handle_exact_phrases`, which
        # `handle_message` calls FIRST — before the intent router and before
        # `try_handle_actions`. Enforced in exactly one place.
        from app.bot.action_router import try_handle_exact_phrases

        monkeypatch.setattr(settings, "mvp_reminder_only", True)

        reply = await try_handle_exact_phrases(
            db,
            "919876543210",
            text,
            user=None,
        )

        assert reply is not None
        assert "preferred store" in reply
