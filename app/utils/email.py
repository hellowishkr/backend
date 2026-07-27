"""Small SMTP email helper for operational lead notifications."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger("wishkr.email")


def _send_email_sync(
    *,
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    reply_to: str | None = None,
) -> bool:
    if not settings.smtp_host:
        logger.warning("email_not_configured subject=%s to=%s", subject, to_email)
        return False

    msg = EmailMessage()
    msg["From"] = settings.smtp_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    # Always set a plain-text part as the fallback; add the HTML alternative so
    # clients that render HTML show the rich version.
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)

    return True


async def send_email(
    *,
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    reply_to: str | None = None,
) -> bool:
    return await asyncio.to_thread(
        _send_email_sync,
        to_email=to_email,
        subject=subject,
        text=text,
        html=html,
        reply_to=reply_to,
    )
