"""Application settings — single source for all env vars."""

from functools import cached_property

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database (default for local dev — overridden by docker-compose in production)
    database_url: str = "postgresql+asyncpg://wishkr:wishkr_dev@localhost:5432/wishkr"

    @field_validator("database_url")
    @classmethod
    def _force_async_pg_driver(cls, v: str) -> str:
        """Normalise bare ``postgresql://`` URLs to the async driver.

        The app uses SQLAlchemy's async engine everywhere — ``app/db/session.py``
        and ADK's ``DatabaseSessionService`` both build via
        ``create_async_engine`` and reject the sync psycopg2 driver. A bare
        ``postgresql://`` URL in ``.env`` (common in dev) would otherwise blow
        up at import time with
        ``InvalidRequestError: The asyncio extension requires an async driver``.

        Non-Postgres URLs (SQLite for tests) pass through untouched.
        """
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v
    # Per-process connection pool. Sized to match downstream concurrency:
    # the worker process runs ``reminder_concurrency`` parallel dispatches,
    # each needing one connection at a time, and the web process serves
    # FastAPI request handlers + the ADK session service (which builds a
    # second engine with the same defaults).
    #
    # 10 + 10 = 20 per process. With:
    #   web worker × 1 (×2 engines) ≈ 40 in flight max
    #   worker (scheduler)          ≈ 20 in flight max
    # …we land safely under Postgres default ``max_connections = 100``
    # while leaving headroom for psql, admin tools, and a second web
    # worker after the Redis migration. Tune ``max_connections`` to 150
    # before bumping ``WEB_WORKERS`` past 2.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # SQL echo — SQLAlchemy prints every query (BEGIN/SELECT/COMMIT/ROLLBACK).
    # Off by default because dev logs were ~85% cron noise. Flip on via
    # ``DB_ECHO_SQL=true`` when you're actively debugging a query path.
    db_echo_sql: bool = False

    # Hubble Money (default is dev API — startup validation catches this in production)
    hubble_client_id: str = ""
    hubble_client_secret: str = ""
    hubble_staging_client_id: str = ""
    hubble_staging_client_secret: str = ""
    hubble_production_client_id: str = ""
    hubble_production_client_secret: str = ""
    hubble_enabled: bool = True
    hubble_env: str = "staging"
    hubble_fail_startup_on_auth_error: bool = False
    hubble_base_url: str = "https://api.dev.myhubble.money"
    # Gift-card SDK store (separate from the bot/order Hubble API).
    hubble_sdk_enabled: bool = False
    hubble_sdk_env: str = "staging"
    hubble_sdk_client_id: str = ""
    hubble_sdk_app_secret: str = ""
    hubble_sdk_private_key: str = ""
    hubble_sdk_mock_token: str = ""
    hubble_sdk_staging_base_url: str = "https://sdk.dev.myhubble.money"
    hubble_sdk_production_base_url: str = "https://sdk.myhubble.money"
    # Wishkr-owned phone number sent to Hubble as support/customer contact for
    # pseudonymised orders. Do not send buyer/recipient PII to Hubble.
    hubble_customer_phone: str = ""

    # WhatsApp Cloud API (Meta direct — no BSP)
    whatsapp_access_token: str = ""                  # permanent token from Meta Business
    whatsapp_phone_number_id: str = ""               # from Meta WhatsApp Business dashboard
    whatsapp_verify_token: str = ""                  # set during Meta webhook registration
    whatsapp_webhook_secret: str = ""                # Meta app secret — required in production
    # When True, voucher confirmations sent outside the 24h customer-service
    # window go via the approved utility templates (`voucher_delivered`,
    # `voucher_delivered_friend`) instead of free-text. Within the window we
    # still use send_text (free). Flip on after both templates are approved
    # AND last_inbound_at backfill is verified.
    whatsapp_use_voucher_template: bool = False

    # Platform fee charged on every gift card order.
    # Displayed to users as "Razorpay payment gateway fee + 18% GST".
    # platform_fee_rate: fraction applied to the gift card face value (default 2%).
    # platform_fee_gst_rate: GST on the fee only, not on the face value (default 18%).
    platform_fee_rate: float = 0.02
    platform_fee_gst_rate: float = 0.18

    # Razorpay (all required in production — validated at startup)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Gemini (Google AI)
    #
    # Always pin a concrete version rather than the `-latest` alias. A silent
    # model swap by Google has historically caused hallucination regressions
    # overnight. Override via env (GEMINI_MODEL=...) when you're ready to bump.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Cheap/low-latency helper model for preflight classification and similar
    # narrow tasks. Keep separate from the main agent model so we can tune cost
    # without weakening tool-heavy bot turns.
    gemini_fast_model: str = "gemini-2.5-flash-lite"
    gemini_temperature: float = 0.4
    # Cap on gemini-2.5-flash's internal reasoning tokens. Left unset the model
    # picks its own budget, and it over-spends badly on this workload: a
    # production turn burned 1399 thinking tokens to emit a 27-token reply and
    # took 7.6s server-side. 512 leaves room for tool-choice reasoning while
    # bounding the tail. 0 disables thinking; -1 restores the dynamic default.
    gemini_thinking_budget: int = 512
    # Fallback classifier when Gemini is unavailable (503/timeout).
    # DeepSeek is OpenAI-compatible — no extra package needed, just httpx.
    deepseek_api_key: str = ""
    deepseek_classifier_model: str = "deepseek-chat"
    gemini_tool_mode: str = "AUTO"

    # Agent settings
    agent_max_tool_rounds: int = 5                   # max tool-call loops per message
    agent_history_window: int = 20                   # messages sent to LLM per call
    agent_stale_timeout_minutes: int = 30            # idle gap before context reset
    message_debounce_seconds: float = 3.0            # wait for user to finish typing

    # LLM cost control
    llm_daily_budget_usd: float = 10.0               # global daily budget (USD)
    llm_monthly_budget_usd: float = 250.0            # global monthly budget (USD)
    llm_per_user_daily_budget_usd: float = 1.0       # per-phone daily limit (USD)
    llm_expensive_conversation_usd: float = 0.75
    llm_expensive_tool_calls: int = 8
    llm_low_cache_hit_ratio: float = 0.05

    # Rate limiting
    rate_limit_messages_per_minute: int = 20       # per phone number
    rate_limit_webhooks_per_minute: int = 100      # per IP address
    max_message_length: int = 4000                 # WhatsApp max is ~4096

    # Public-launch user cap
    # -----------------------
    # Max number of registered users (rows in `users`) during the limited
    # launch. New sign-ups beyond this get the waitlist message instead of a
    # user row; existing users are never affected. Set 0 to disable the cap
    # (i.e. once you scale). Raise the number on each server to grow the limit.
    launch_user_cap: int = 200
    waitlist_message: str = (
        "Hi there! 👋 Thank you so much for reaching out to Wishkr 🎁\n\n"
        "We've just opened to a small first batch of users and every spot is "
        "currently full 🙏 You're on our waitlist now, and we'll message you "
        "right here the moment a place opens up.\n\n"
        "We can't wait to help you find wonderful gifts very soon. Thank you "
        "for your patience and kindness! 💛"
    )

    # Redis — shared cross-worker state (rate limits, outbound reply dedup,
    # ACK cooldowns). Empty string = not configured: everything falls back to
    # the original in-memory behaviour, which is only safe with WEB_WORKERS=1.
    redis_url: str = ""

    # Reminder cron throughput
    # ------------------------
    # ``reminder_batch_size`` is the max number of due reminders fetched per
    # tick. ``reminder_concurrency`` is the max number of dispatches running
    # in parallel inside that tick. Total sustained send rate is roughly
    # ``concurrency / avg_send_latency_seconds`` — at concurrency=15 and
    # ~300 ms per Meta call, that's ~50 sends/sec, capped by Meta's tier
    # well before any hardware limit.
    #
    # Sized for the current 3-server prod layout (1 DB server, 1 web, 1
    # worker; pool_size=5 + max_overflow=10 = 15 conns per process). The
    # worker process at concurrency=15 will hold up to 15 conns concurrently
    # — exactly matching the pool. Lower this if you reduce ``db_pool_size``.
    reminder_batch_size: int = 300
    reminder_concurrency: int = 15
    # Cron tick frequency. Lower = lower max latency between scheduled_at
    # and actual send; higher = less DB churn from polling empty results.
    reminder_tick_minutes: int = 5
    # India operational window for the scheduler worker (IST 24h clock).
    # This is intentionally separate from recipient-local quiet hours below:
    # outside this window the whole reminder tick pauses for operational
    # reasons, regardless of recipient timezone.
    reminder_window_start_hour_ist: int = 6
    reminder_window_start_minute_ist: int = 30
    reminder_window_end_hour_ist: int = 22
    # Recipient-local dispatch window. A due reminder outside this window
    # stays PENDING and is excluded from the dispatch batch until the window
    # opens. The recipient is target_phone for friend reminders, otherwise
    # the occasion owner.
    reminder_recipient_window_start_hour: int = 6
    reminder_recipient_window_start_minute: int = 30
    reminder_recipient_window_end_hour: int = 22

    # Feature flags — default false for production safety, enable in staging only
    # Wishlist sharing (get_friend_wishlist, reserve, purchase)
    enable_shared_wishlist: bool = False            # expose wishlist sharing tools to agent
    # Recipient preference collection
    preference_outbound_enabled: bool = False       # allow request_recipient_preferences to send WhatsApp messages
    preference_interceptor_enabled: bool = False    # intercept inbound messages for preference replies
    # Public launch safety: reminder-first MVP. Marketplace/payment code stays
    # in repo, but production cannot expose or execute it while this is true.
    mvp_reminder_only: bool = True
    # Optional low-cost review of deterministic gift suggestions. DeepSeek is
    # primary; Gemini Flash-Lite is the fallback.
    gift_refinement_enabled: bool = False
    wishkr_website_url: str = "https://www.wishkr.com"

    # App
    app_role: str = "web"
    run_scheduler: bool = False
    # Empty default — set via FOUNDER_PHONE env var. Production startup
    # validation crashes if missing; dev logs an error and drops alerts.
    # Hardcoding the founder's number in source means it leaks via git
    # history if the repo is ever public-mirrored or contractor-shared.
    founder_phone: str = ""
    corporate_lead_email_to: str = "hello@wishkr.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "hello@wishkr.com"
    smtp_use_tls: bool = True
    # Internal/admin endpoints (ops/admin only). Keep empty to disable access.
    admin_api_token: str = ""
    # Purpose-scoped secret for signing short-lived product-preview links. Shared
    # with the admin app, which mints the tokens. Distinct from admin_api_token so
    # a preview link only grants read of one product, never admin write access.
    preview_signing_secret: str = ""
    # Owner-of-last-resort login for the admin panel (Phase 4). Lets the founder
    # log in by email+password even before any admin_users row exists.
    admin_owner_email: str = ""
    admin_api_password_fallback: str = ""
    bot_product_catalog_enabled: bool = False
    r2_endpoint: str = ""
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_public_base_url: str = ""
    app_env: str = "development"
    allowed_hosts: str = "api.wishkr.com,wishkr.com,www.wishkr.com,127.0.0.1,localhost"

    # Sentry error tracking. DSN comes from the project you created in Sentry.
    # If empty, Sentry init is a no-op — local dev runs silently without
    # needing an account. Release tag (``git_sha``) lets Sentry group
    # regressions by deploy.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.05           # 5% perf sampling — cheap
    sentry_enable_debug_route: bool = False           # set true temporarily to smoke /sentry-debug
    git_sha: str = ""                                 # injected by Dockerfile build arg

    model_config = {"env_file": ".env", "extra": "ignore"}

    @cached_property
    def allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @cached_property
    def hubble_effective_base_url(self) -> str:
        if self.hubble_base_url:
            return self.hubble_base_url.rstrip("/")
        if self.hubble_env == "production":
            return "https://api.myhubble.money"
        return "https://api.dev.myhubble.money"

    @cached_property
    def hubble_sdk_effective_base_url(self) -> str:
        if self.hubble_sdk_env == "production":
            return self.hubble_sdk_production_base_url.rstrip("/")
        return self.hubble_sdk_staging_base_url.rstrip("/")

    @cached_property
    def hubble_effective_client_id(self) -> str:
        if self.hubble_env == "production":
            return self.hubble_production_client_id or self.hubble_client_id
        return self.hubble_staging_client_id or self.hubble_client_id

    @cached_property
    def hubble_effective_client_secret(self) -> str:
        if self.hubble_env == "production":
            return self.hubble_production_client_secret or self.hubble_client_secret
        return self.hubble_staging_client_secret or self.hubble_client_secret


settings = Settings()
