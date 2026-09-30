import logging
import os
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings

# Initialise Sentry as early as possible — before any SQLAlchemy, HTTPX
# or FastAPI import would finalise. The SDK's auto-integrations patch
# those libraries at init time, so earlier = more coverage. No-op when
# SENTRY_DSN is empty (local dev).
from app.core.observability import init_sentry  # noqa: E402
init_sentry(settings)

# ADK reads its key from the environment. google-genai accepts either
# GEMINI_API_KEY or GOOGLE_API_KEY, and warns on every client construction when
# both are set ("Both GOOGLE_API_KEY and GEMINI_API_KEY are set"). Deployments
# provide GEMINI_API_KEY, so only fill in GOOGLE_API_KEY when it is genuinely
# absent — e.g. a local shell that exported neither.
if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = settings.gemini_api_key

_is_prod = settings.app_env == "production"
_SCHEDULER_LOCK_KEY = 41051


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("type", "phone", "duration_ms", "bucket", "breaker"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    if _is_prod:
        handler.setFormatter(JsonFormatter())
        wishkr_level = logging.INFO
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        wishkr_level = logging.DEBUG

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("wishkr").setLevel(wishkr_level)


_configure_logging()
logger = logging.getLogger("wishkr.main")


def _validate_config():
    """Fail-closed: crash at startup if critical config is missing.

    In development, only gemini_api_key is required (for testing the AI agent).
    In production, ALL secrets must be configured — no silent degradation.
    """
    _is_prod = settings.app_env == "production"

    # Always required (app is useless without these)
    always_required = {
        "gemini_api_key": settings.gemini_api_key,
    }

    # Required in production (money flows through these)
    prod_required = {
        "whatsapp_access_token": settings.whatsapp_access_token,
        "whatsapp_phone_number_id": settings.whatsapp_phone_number_id,
        "whatsapp_verify_token": settings.whatsapp_verify_token,
        "whatsapp_webhook_secret": settings.whatsapp_webhook_secret,
        "razorpay_key_id": settings.razorpay_key_id,
        "razorpay_key_secret": settings.razorpay_key_secret,
        "razorpay_webhook_secret": settings.razorpay_webhook_secret,
    }
    if settings.hubble_enabled:
        prod_required["hubble_client_id"] = settings.hubble_effective_client_id
        prod_required["hubble_client_secret"] = settings.hubble_effective_client_secret
        prod_required["hubble_customer_phone"] = settings.hubble_customer_phone

    missing = [k for k, v in always_required.items() if not v]
    if _is_prod:
        missing += [k for k, v in prod_required.items() if not v]

    if missing:
        raise RuntimeError(
            f"Missing required config: {', '.join(missing)}. "
            f"Set these in .env or environment variables."
        )

    # Most gift vibes now rely on the DeepSeek-first, Gemini-fallback
    # refinement path; only the small cold-start library works without it.
    # Keep local/test and the scheduler worker free to run with the flag off.
    if (
        _is_prod
        and settings.app_role == "web"
        and not settings.gift_refinement_enabled
    ):
        raise RuntimeError(
            "GIFT_REFINEMENT_ENABLED must be true for the production web/API server."
        )

    if (
        _is_prod
        and settings.hubble_enabled
        and settings.hubble_env == "production"
        and settings.hubble_effective_base_url.startswith("https://api.dev.")
    ):
        raise RuntimeError(
            "HUBBLE_BASE_URL points to dev API in production! "
            "Set HUBBLE_BASE_URL=https://api.myhubble.money"
        )
    if settings.hubble_env not in {"staging", "production"}:
        raise RuntimeError("HUBBLE_ENV must be 'staging' or 'production'")
    if _is_prod and not settings.allowed_hosts_list:
        raise RuntimeError("ALLOWED_HOSTS must contain at least one host in production")

    # founder_phone gates EVERY founder alert (paid-after-expiry, dispute,
    # refund-fail, DLQ, voucher-send-fail, Meta 401). A misconfigured value
    # means alerts silently drop in send_founder_alert. Crash startup so the
    # operator is forced to fix it before money flows.
    if _is_prod:
        from app.utils.alerts import is_valid_founder_phone
        if not is_valid_founder_phone(settings.founder_phone):
            raise RuntimeError(
                "FOUNDER_PHONE missing or malformed in production. "
                "Set FOUNDER_PHONE to E.164 digits without '+' (e.g. 9198XXXXXXXX)."
            )


async def _try_acquire_scheduler_lock():
    """Ensure only one replica runs APScheduler jobs."""
    from app.db.session import engine

    conn = await engine.connect()
    backend_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
    got_lock = bool(
        await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCHEDULER_LOCK_KEY})
    )
    if got_lock:
        # SQLAlchemy autobegins a transaction for the SELECT above.  The lock
        # is session-scoped, so committing keeps it held while preventing the
        # connection from sitting "idle in transaction" until PostgreSQL's
        # hardened timeout terminates the session and silently drops the lock.
        await conn.commit()
        logger.info(
            "Scheduler advisory lock acquired lock_key=%s worker_pid=%s pg_backend_pid=%s",
            _SCHEDULER_LOCK_KEY,
            os.getpid(),
            backend_pid,
        )
        return conn
    logger.info(
        "Scheduler advisory lock not acquired lock_key=%s worker_pid=%s pg_backend_pid=%s",
        _SCHEDULER_LOCK_KEY,
        os.getpid(),
        backend_pid,
    )
    await conn.close()
    return None


async def _release_scheduler_lock(conn) -> None:
    """Release the scheduler advisory lock on shutdown.

    Safety: If the connection is already closed (DB restart, network blip),
    Postgres automatically releases advisory locks when the session dies.
    We log a warning but do not raise — this is expected during ungraceful
    shutdown and should not trigger Sentry alerts.
    """
    if conn is None:
        return

    try:
        if conn.closed:
            logger.warning(
                "Scheduler lock connection already closed lock_key=%s worker_pid=%s; "
                "Postgres will auto-release on session end",
                _SCHEDULER_LOCK_KEY,
                os.getpid(),
            )
            return

        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHEDULER_LOCK_KEY})
        await conn.commit()
        logger.info(
            "Scheduler advisory lock released lock_key=%s worker_pid=%s",
            _SCHEDULER_LOCK_KEY,
            os.getpid(),
        )
    except Exception as e:
        from app.utils.db_retry import is_transient_db_error

        if is_transient_db_error(e):
            logger.warning(
                "Scheduler lock release failed (transient, Postgres will auto-release) "
                "lock_key=%s worker_pid=%s: %s",
                _SCHEDULER_LOCK_KEY,
                os.getpid(),
                e,
            )
        else:
            logger.exception(
                "Scheduler lock release failed unexpectedly lock_key=%s worker_pid=%s",
                _SCHEDULER_LOCK_KEY,
                os.getpid(),
            )
    finally:
        try:
            if not conn.closed:
                await conn.close()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate config FIRST — fail fast if misconfigured
    _validate_config()

    # Startup
    from app.services.hubble import HubbleUnavailableError, hubble_service
    if settings.hubble_enabled and settings.app_env == "production":
        try:
            await hubble_service.authenticate()
        except HubbleUnavailableError as e:
            if settings.hubble_fail_startup_on_auth_error:
                raise
            logger.warning(
                "Hubble unavailable at startup; continuing because HUBBLE_FAIL_STARTUP_ON_AUTH_ERROR=false: %s",
                e.reason,
            )
    else:
        if not settings.hubble_enabled:
            hubble_service.mark_unavailable("Hubble integration is disabled")
            logger.warning("Hubble disabled; startup will continue without Hubble")
        else:
            logger.info("Skipping Hubble auth in %s", settings.app_env)

    from app.jobs.scheduler import start_scheduler, stop_scheduler
    scheduler_lock = None
    app.state.scheduler_started = False
    if settings.run_scheduler:
        scheduler_lock = await _try_acquire_scheduler_lock()
        app.state.scheduler_lock = scheduler_lock
        app.state.scheduler_started = scheduler_lock is not None
    else:
        app.state.scheduler_lock = None
        logger.info("RUN_SCHEDULER=false; scheduler disabled for app_role=%s", settings.app_role)

    if scheduler_lock is not None:
        start_scheduler()
        logger.info("Scheduler lock acquired; jobs enabled")
    elif settings.run_scheduler:
        logger.info("Scheduler lock unavailable; jobs disabled on this replica")
    else:
        logger.info("Scheduler intentionally disabled on this replica")

    # Startup sanity: warn loudly if founder_phone is malformed. We're
    # ``send_founder_alert``'s only listener; a typo'd phone silently drops
    # every paid-after-expiry, dispute, refund-failed, and DLQ alert.
    from app.utils.alerts import is_valid_founder_phone
    if not is_valid_founder_phone(settings.founder_phone):
        logger.error(
            "founder_phone is missing or malformed (got %r); founder alerts will be DROPPED",
            settings.founder_phone,
        )

    logger.info("Wishkr backend started env=%s", settings.app_env)
    yield

    # Shutdown — cancel in-flight WhatsApp background tasks first so they
    # don't keep writing to a closing DB pool, then stop the scheduler.
    from app.webhooks.whatsapp import drain_active_tasks
    await drain_active_tasks()
    # After in-flight turns are drained — the shared intent-router HTTP client
    # is still in use until they finish.
    from app.bot.intent_router import aclose as close_intent_router
    await close_intent_router()
    if getattr(app.state, "scheduler_started", False):
        stop_scheduler()
    await _release_scheduler_lock(getattr(app.state, "scheduler_lock", None))
    logger.info("Wishkr backend shutting down")

app = FastAPI(
    title="Wishkr Backend",
    version="0.1.0",
    lifespan=lifespan,
    # Disable interactive API docs in production
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# CORS — only allow our frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wishkr.com", "https://www.wishkr.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)


if _is_prod:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts_list,
    )


# Tight CSP for a JSON API — it has no HTML UI of its own. Blocks any rendered
# content (including an accidentally-served admin view of raw lead notes)
# from loading scripts, styles, or framing the API.
_API_CSP = (
    "default-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


from app.api.audit_middleware import audit_admin_writes  # noqa: E402
from app.api.catalog_cache_middleware import invalidate_catalog_cache  # noqa: E402

app.middleware("http")(audit_admin_writes)
app.middleware("http")(invalidate_catalog_cache)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _API_CSP
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if _is_prod:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.head('/health')
async def health_head():
    return Response(status_code=200)

# Sentry smoke-test endpoint. Gated behind `SENTRY_ENABLE_DEBUG_ROUTE=true`
# so it doesn't sit exposed in production forever. Hit once after deploy
# to confirm the DSN + integration are wired, then flip the flag off.
if settings.sentry_enable_debug_route:
    @app.get("/sentry-debug")
    async def _sentry_debug():
        raise RuntimeError("sentry-debug: deliberate test exception")


# Register routers
from app.webhooks.whatsapp import router as whatsapp_router  # noqa: E402
from app.webhooks.razorpay import router as razorpay_router  # noqa: E402
from app.api.vendor_leads import router as vendor_leads_router  # noqa: E402
from app.api.corporate_leads import router as corporate_leads_router  # noqa: E402
from app.api.vendor_app_downloads import router as vendor_app_downloads_router  # noqa: E402
from app.api.feedback import router as feedback_router  # noqa: E402
from app.api.cards import router as cards_router  # noqa: E402
from app.api.internal import router as internal_router  # noqa: E402
from app.api.catalog import router as catalog_router  # noqa: E402
from app.api.admin_catalog import router as admin_catalog_router  # noqa: E402
from app.api.admin_orders import router as admin_orders_router  # noqa: E402
from app.api.admin_customers import router as admin_customers_router  # noqa: E402
from app.api.admin_occasions import router as admin_occasions_router  # noqa: E402
from app.api.admin_ai_usage import router as admin_ai_usage_router  # noqa: E402
from app.api.admin_campaigns import router as admin_campaigns_router  # noqa: E402
from app.api.admin_leads import router as admin_leads_router  # noqa: E402
from app.api.admin_settings import router as admin_settings_router  # noqa: E402
from app.api.admin_blog import router as admin_blog_router  # noqa: E402
from app.api.product_orders import router as product_orders_router  # noqa: E402
from app.api.vendor_edit import router as vendor_edit_router  # noqa: E402
from app.api.vendor_edit import admin_router as vendor_edit_admin_router  # noqa: E402

app.include_router(whatsapp_router, prefix="/webhooks")
app.include_router(razorpay_router, prefix="/webhooks")
app.include_router(vendor_leads_router, prefix="/api")
app.include_router(corporate_leads_router, prefix="/api")
app.include_router(vendor_app_downloads_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(cards_router, prefix="/api")
app.include_router(catalog_router, prefix="/api")
app.include_router(admin_catalog_router, prefix="/api")
app.include_router(admin_orders_router, prefix="/api")
app.include_router(admin_customers_router, prefix="/api")
app.include_router(admin_occasions_router, prefix="/api")
app.include_router(admin_ai_usage_router, prefix="/api")
app.include_router(admin_campaigns_router, prefix="/api")
app.include_router(admin_leads_router, prefix="/api")
app.include_router(admin_settings_router, prefix="/api")
app.include_router(admin_blog_router, prefix="/api")
app.include_router(product_orders_router, prefix="/api")
app.include_router(vendor_edit_router, prefix="/api")
app.include_router(vendor_edit_admin_router, prefix="/api")
from app.api.mobile_auth import router as mobile_auth_router  # noqa: E402
app.include_router(mobile_auth_router, prefix="/api")
from app.api.mobile_occasions import router as mobile_occasions_router  # noqa: E402
app.include_router(mobile_occasions_router, prefix="/api")
from app.api.mobile_wishlist import router as mobile_wishlist_router  # noqa: E402
app.include_router(mobile_wishlist_router, prefix="/api")
from app.api.mobile_preferences import router as mobile_preferences_router  # noqa: E402
app.include_router(mobile_preferences_router, prefix="/api")
from app.api.mobile_calendar import router as mobile_calendar_router  # noqa: E402
app.include_router(mobile_calendar_router, prefix="/api")
from app.api.mobile_notifications import router as mobile_notifications_router  # noqa: E402
app.include_router(mobile_notifications_router, prefix="/api")
from app.api.mobile_discovery import router as mobile_discovery_router  # noqa: E402
app.include_router(mobile_discovery_router, prefix="/api")
app.include_router(internal_router)


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
async def assetlinks_endpoint():
    """Android Digital Asset Links verification for app links."""
    from fastapi.responses import JSONResponse
    return JSONResponse(content=[
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.example.wishkr_app",
                "sha256_cert_fingerprint_list": [
                    "EA:4F:41:87:DD:DF:EF:2B:68:15:91:99:A8:A5:62:B4:E9:9D:61:1A:4A:B9:57:1A:05:2B:D7:1E:32:37:79:E1"
                ]
            }
        }
    ])


# Local agent chat window (GET/POST /dev/whatsapp-chat). Registered only outside
# production: it runs the agent as an arbitrary phone with no signature check,
# so in production the route must not exist at all rather than be guarded.
if (settings.app_env or "development").lower() != "production":
    from app.api.dev_chat import router as dev_chat_router  # noqa: E402

    app.include_router(dev_chat_router)
    logger.info("dev chat window enabled at /dev/whatsapp-chat (APP_ENV=%s)", settings.app_env)
