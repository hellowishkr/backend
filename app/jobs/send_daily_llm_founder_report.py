"""Send daily LLM spend report to founder."""

import logging

from app.db.session import async_session
from app.utils.alerts import send_founder_alert
from app.utils.llm_founder_report import build_daily_founder_llm_report, format_founder_llm_report

logger = logging.getLogger("wishkr.jobs.daily_llm_report")


async def send_daily_llm_founder_report() -> None:
    try:
        async with async_session() as db:
            report = await build_daily_founder_llm_report(db)
        await send_founder_alert(format_founder_llm_report(report))
        logger.info("daily LLM founder report sent")
    except Exception as e:
        logger.exception("daily LLM founder report failed: %s", e)
