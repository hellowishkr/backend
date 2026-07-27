"""Standalone scheduler entrypoint for the production worker server."""

import asyncio
import logging
import os
import signal
from contextlib import suppress

from sqlalchemy import text

from app.core.config import settings
from app.core.observability import init_sentry
from app.db.session import engine
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.main import _configure_logging, _try_acquire_scheduler_lock, _release_scheduler_lock, _validate_config


logger = logging.getLogger("wishkr.scheduler_worker")


async def _wait_for_shutdown() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()


async def main() -> None:
    _configure_logging()
    init_sentry(settings)
    _validate_config()

    async with engine.connect() as conn:
        await conn.scalar(text("SELECT 1"))

    if not settings.run_scheduler:
        raise RuntimeError("RUN_SCHEDULER must be true for app.scheduler_worker")

    scheduler_lock = await _try_acquire_scheduler_lock()
    if scheduler_lock is None:
        raise RuntimeError("Could not acquire scheduler advisory lock")

    start_scheduler()
    with open("/tmp/wishkr-scheduler-worker.ready", "w", encoding="utf-8") as ready_file:
        ready_file.write("ready\n")
    logger.info("Standalone scheduler worker started app_role=%s pid=%s", settings.app_role, os.getpid())
    try:
        await _wait_for_shutdown()
    finally:
        stop_scheduler()
        await _release_scheduler_lock(scheduler_lock)
        await engine.dispose()
        with suppress(FileNotFoundError):
            os.remove("/tmp/wishkr-scheduler-worker.ready")
        logger.info("Standalone scheduler worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
