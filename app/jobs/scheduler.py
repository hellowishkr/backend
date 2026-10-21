"""APScheduler setup — runs cron jobs for reminders, stale orders, etc."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.jobs.send_reminders import send_due_reminders
from app.jobs.poll_stale_orders import poll_stale_orders
from app.jobs.deliver_gifts import deliver_scheduled_gifts
from app.jobs.cleanup_rate_limiters import cleanup_rate_limiters
from app.jobs.retry_failed_outbound import retry_failed_outbound
from app.jobs.process_razorpay_refunds import process_pending_refunds
from app.jobs.sync_gift_catalog import sync_gift_catalog
from app.jobs.reconcile_payments import reconcile_payments
from app.jobs.topup_link_reconciliation import topup_link_reconciliation
from app.jobs.wishlist_lifecycle import send_wishlist_reminders
from app.jobs.send_daily_llm_founder_report import send_daily_llm_founder_report
from app.jobs.send_monthly_summary import send_monthly_occasion_summaries

logger = logging.getLogger("wishkr.scheduler")

scheduler = AsyncIOScheduler()


def _on_job_error(event):
    """APScheduler catches job exceptions before they bubble, so the
    FastAPI integration never sees them. Forward to Sentry explicitly.

    Groups by ``job_id`` via the ``cron_job`` tag so each recurring job
    gets its own issue stream in Sentry instead of all crons merging
    into one noisy alert.
    """
    if event.exception is None:
        return
    logger.error("Cron job %s raised: %s", event.job_id, event.exception)
    try:
        import sentry_sdk
        from app.core.observability import capture_exception as _sentry_capture
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("cron_job", event.job_id or "unknown")
            scope.set_tag("error_class", "cron_failure")
            _sentry_capture(event.exception, job_id=event.job_id)
    except Exception:
        logger.exception("Failed to forward cron failure to Sentry")


scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)


def start_scheduler():
    """Configure and start all scheduled jobs."""

    # Send due reminders. Tick frequency comes from settings so we can tune
    # it per-env without code changes (default 5 min; was 15 min pre-2026-05).
    # ``max_instances=1`` + ``coalesce`` prevent two ticks from picking up
    # the same row when one run overruns (would otherwise double-send). The
    # job itself is internally bounded by REMINDER_BATCH_SIZE, the India
    # operational window, and per-recipient quiet hours — see
    # ``send_due_reminders``.
    scheduler.add_job(
        send_due_reminders,
        "interval",
        minutes=settings.reminder_tick_minutes,
        id="send_reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    if not settings.mvp_reminder_only:
        # Poll stale orders every 5 minutes
        scheduler.add_job(
            poll_stale_orders,
            "interval",
            minutes=5,
            id="poll_stale_orders",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # Deliver scheduled gifts every 15 minutes (celebrant flow)
        scheduler.add_job(
            deliver_scheduled_gifts,
            "interval",
            minutes=15,
            id="deliver_gifts",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    # Sweep stale keys out of in-memory rate limiters every hour
    scheduler.add_job(
        cleanup_rate_limiters,
        "interval",
        hours=1,
        id="cleanup_rate_limiters",
        replace_existing=True,
    )

    scheduler.add_job(
        retry_failed_outbound,
        "interval",
        seconds=15,
        id="retry_failed_outbound",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    scheduler.add_job(
        process_pending_refunds,
        "interval",
        seconds=15,
        id="process_razorpay_refunds",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Reconcile stuck/failed top-up payment-link creations and cancellations.
    # 20s (not 15s like retry_failed_outbound) since each stuck row costs a
    # Razorpay HTTP call, and the grace period is 60s — no benefit ticking
    # faster than roughly a third of that window.
    scheduler.add_job(
        topup_link_reconciliation,
        "interval",
        seconds=20,
        id="topup_link_reconciliation",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    if not settings.mvp_reminder_only:
        # Mirror partner catalogues (Hubble, later Xoxoday, …) into gift_catalog
        # at 06:00 IST. `suggest_gifts` reads from the mirror — bot never blocks
        # on a live partner API during a WhatsApp turn. Payments still go live.
        scheduler.add_job(
            sync_gift_catalog,
            "cron",
            hour=6,
            minute=0,
            timezone="Asia/Kolkata",
            id="sync_gift_catalog",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    # Wishlist reminders run hourly; the job sends only during each user's
    # local 08:00 hour and applies their local calendar date before LIMIT.
    scheduler.add_job(
        send_wishlist_reminders,
        "cron",
        minute=0,
        timezone="UTC",
        id="wishlist_reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Founder ops report — daily LLM spend/cost anomalies at 09:00 IST.
    scheduler.add_job(
        send_daily_llm_founder_report,
        "cron",
        hour=9,
        minute=0,
        timezone="Asia/Kolkata",
        id="daily_llm_founder_report",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Monthly occasion summary — invoked hourly on UTC day 1 and the preceding
    # UTC month-end. That covers local 09:00 on day 1 for every IANA offset
    # without scanning users every day. The job owns the exact local-time gate
    # and Redis-marker dedupe.
    scheduler.add_job(
        send_monthly_occasion_summaries,
        "cron",
        day="1,last",
        minute=0,
        timezone="UTC",
        id="monthly_occasion_summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


    if not settings.mvp_reminder_only:
        # Hourly reconciliation: find Razorpay-paid orders whose Hubble voucher
        # never landed (lost webhook, partial outage). Founder alert only — we
        # do not auto-replay Hubble from here.
        scheduler.add_job(
            reconcile_payments,
            "interval",
            hours=1,
            id="reconcile_payments",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        # One-shot catalog sync 30s after startup — handles first boot (empty
        # table) and every deploy (refresh with latest partner changes). Cheap:
        # one paginated fetch, runs in background, never blocks requests.
        scheduler.add_job(
            sync_gift_catalog,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=30),
            id="sync_gift_catalog_boot",
            replace_existing=True,
            max_instances=1,
        )

    scheduler.start()
    if settings.mvp_reminder_only:
        logger.info("Scheduler started in MVP reminder-only mode: reminders (15min), rate-limiter cleanup (1h), outbound retry (15s), topup link reconciliation (20s), wishlist reminders (08:30 IST), founder report (09:00 IST), monthly summary (1st 09:30 IST)")
    else:
        logger.info("Scheduler started: reminders (15min), stale orders (5min), gift delivery (15min), rate-limiter cleanup (1h), outbound retry (15s), topup link reconciliation (20s), catalog sync (06:00 IST), wishlist reminders (08:30 IST), founder report (09:00 IST), monthly summary (1st 09:30 IST), payment reconciliation (1h)")


def stop_scheduler():
    """Shutdown the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
