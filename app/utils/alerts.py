"""Founder alerts — send WhatsApp messages to founder for critical events."""

import logging
import re

from app.core.config import settings

logger = logging.getLogger("wishkr.alerts")


# Meta accepts E.164 without "+"; we expect 10–15 digits (country + subscriber).
# An empty/short founder_phone means founder alerts silently drop, which has
# been a real-world failure mode (typo, missing env var). Validate at startup.
_VALID_FOUNDER_PHONE = re.compile(r"^\d{10,15}$")


def is_valid_founder_phone(phone: str | None) -> bool:
    if not phone:
        return False
    return bool(_VALID_FOUNDER_PHONE.match(phone))


async def send_founder_alert(message: str):
    """Send an alert to the founder's WhatsApp number.

    Free text only delivers inside the founder's 24h CS window; outside it
    Meta silently drops the message (we lost daily LLM reports and dispute
    alerts this way, June 2026). Look up the founder's last inbound and fall
    back to the ``account_notification`` template when the window is closed.
    """
    if not is_valid_founder_phone(settings.founder_phone):
        logger.error(
            "Founder alert dropped — invalid founder_phone configured (len=%d)",
            len(settings.founder_phone or ""),
        )
        return
    try:
        from sqlalchemy import select

        from app.bot.whatsapp_templates import TEMPLATE_ACCOUNT_NOTIFICATION, _first_name
        from app.db.session import async_session
        from app.models import User
        from app.services.whatsapp import send_text_or_template

        async with async_session() as db:
            row = (await db.execute(
                select(User.name, User.last_inbound_at)
                .where(User.phone == settings.founder_phone)
            )).first()

        await send_text_or_template(
            settings.founder_phone,
            message,
            last_inbound_at=row.last_inbound_at if row else None,
            template_name=TEMPLATE_ACCOUNT_NOTIFICATION,
            body_params=[_first_name(row.name if row else None), message],
        )
        logger.info("Founder alert sent len=%d", len(message))
    except Exception as e:
        # If sending fails, at least log it
        logger.error("Failed to send founder alert: %s | message_len=%d", e, len(message))
