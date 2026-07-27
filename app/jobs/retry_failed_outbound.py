"""Retry durable failed outbound WhatsApp messages."""

import json
import logging
import time as _time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from app.db.session import async_session
from app.models import FailedOutboundMessage, User
from app.services.whatsapp import is_in_cs_window, whatsapp_service
from app.utils.alerts import send_founder_alert
from app.utils.db_retry import is_transient_db_error, retry_db_operation

logger = logging.getLogger("wishkr.retry_failed_outbound")

_MAX_RETRIES = 3
_DLQ_ALERT_THRESHOLD = 10
_DLQ_ALERT_INTERVAL_SECONDS = 3600
_last_dlq_alert_ts: float = 0.0


def _is_retry_due(row: FailedOutboundMessage, now: datetime) -> bool:
    """Simple retry backoff to avoid hammering WhatsApp during outages."""
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age = now - created_at
    if row.retry_count == 0:
        return True
    if row.retry_count == 1:
        return age > timedelta(seconds=30)
    if row.retry_count == 2:
        return age > timedelta(seconds=60)
    return False


async def _load_pending_rows(cutoff: datetime) -> list[FailedOutboundMessage]:
    """Load pending failed outbound rows with transient DB retry."""

    async def _do_load() -> list[FailedOutboundMessage]:
        async with async_session() as db:
            result = await db.execute(
                select(FailedOutboundMessage)
                .where(
                    FailedOutboundMessage.retry_count < _MAX_RETRIES,
                    or_(
                        FailedOutboundMessage.retry_count == 0,
                        FailedOutboundMessage.created_at <= cutoff,
                    ),
                )
                .order_by(FailedOutboundMessage.created_at.asc())
            )
            return list(result.scalars().all())

    return await retry_db_operation(_do_load, label="retry_outbound_load")


async def _process_single_row(row_id: str, now: datetime) -> None:
    """Process a single failed outbound row in its own session.

    Uses a fresh session per row so a DB blip mid-batch doesn't lose progress.
    The WhatsApp send is NOT retried here — if it fails, we increment
    retry_count and let the next job run pick it up.
    """
    async with async_session() as db:
        row = await db.get(FailedOutboundMessage, row_id)
        if row is None:
            return

        if not _is_retry_due(row, now):
            return

        send_succeeded = False
        try:
            if row.template_name:
                try:
                    params = json.loads(row.template_params_json or "{}")
                except json.JSONDecodeError:
                    params = {}
                await whatsapp_service.send_template(
                    row.phone,
                    row.template_name,
                    header_params=params.get("header_params"),
                    body_params=params.get("body_params"),
                )
            else:
                # Free-text retries only deliver inside the recipient's 24h
                # CS window. Outside it Meta returns 200 + wamid but drops
                # the message — we'd then delete the row believing it was
                # delivered. Park the row in the DLQ instead so the
                # pile-up alert surfaces it for manual replay.
                inbound_row = (await db.execute(
                    select(User.last_inbound_at).where(User.phone == row.phone)
                )).first()
                if not inbound_row or not is_in_cs_window(inbound_row.last_inbound_at):
                    row.retry_count = _MAX_RETRIES
                    row.error_message = (
                        "cs_window_closed: free-text retry would be silently "
                        "dropped by Meta; needs template or manual replay"
                    )
                    logger.warning(
                        "retry_failed_outbound parked row (CS window closed) phone=%s kind=%s",
                        row.phone, row.send_kind,
                    )
                    await db.commit()
                    return
                await whatsapp_service.send_text(row.phone, row.message_body)
            send_succeeded = True
        except Exception as e:
            row.retry_count += 1
            row.error_message = str(e)[:1000]
            logger.warning(
                "retry_failed_outbound send failed phone=%s kind=%s retry_count=%d err=%s",
                row.phone,
                row.send_kind,
                row.retry_count,
                e,
            )

        if send_succeeded:
            await db.delete(row)

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            if is_transient_db_error(e):
                logger.warning(
                    "retry_failed_outbound DB commit failed (transient) row_id=%s: %s",
                    row_id,
                    e,
                )
            else:
                logger.exception(
                    "retry_failed_outbound DB commit failed row_id=%s",
                    row_id,
                )


async def _count_stuck_rows() -> int:
    """Count rows that have exhausted all retries."""

    async def _do_count() -> int:
        async with async_session() as db:
            result = await db.execute(
                select(func.count(FailedOutboundMessage.id))
                .where(FailedOutboundMessage.retry_count >= _MAX_RETRIES)
            )
            return result.scalar_one()

    return await retry_db_operation(_do_count, label="retry_outbound_dlq_count")


async def retry_failed_outbound() -> None:
    """Best-effort replay for failed outbound sends.

    Hardened for transient DB disconnects:
    - If DB unavailable at job start, logs warning and exits cleanly
    - Uses fresh session per row to avoid mid-batch connection loss
    - Does not duplicate WhatsApp sends (each row processed once per job run)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=60)

    try:
        rows = await _load_pending_rows(cutoff)
    except Exception as e:
        if is_transient_db_error(e):
            logger.warning(
                "retry_failed_outbound skipped: DB unavailable (transient): %s",
                e,
            )
            return
        logger.exception("retry_failed_outbound failed to load rows")
        raise

    for row in rows:
        try:
            await _process_single_row(row.id, now)
        except Exception as e:
            if is_transient_db_error(e):
                logger.warning(
                    "retry_failed_outbound row processing skipped (transient) row_id=%s: %s",
                    row.id,
                    e,
                )
            else:
                logger.exception(
                    "retry_failed_outbound row processing failed row_id=%s",
                    row.id,
                )

    try:
        stuck_count = await _count_stuck_rows()
    except Exception as e:
        logger.warning("retry_failed_outbound DLQ count failed: %s", e)
        return

    if stuck_count and stuck_count >= _DLQ_ALERT_THRESHOLD:
        global _last_dlq_alert_ts
        if _time.time() - _last_dlq_alert_ts >= _DLQ_ALERT_INTERVAL_SECONDS:
            _last_dlq_alert_ts = _time.time()
            try:
                await send_founder_alert(
                    f"⚠️ Outbound DLQ pile-up: {stuck_count} messages exhausted "
                    f"all retries. Inspect failed_outbound_messages and replay "
                    f"manually or wipe."
                )
            except Exception:
                logger.exception("Failed to send DLQ alert")
