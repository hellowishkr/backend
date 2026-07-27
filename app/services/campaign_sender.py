"""Backend-driven acquisition-campaign sender.

Sends an approved Meta WhatsApp acquisition/welcome template to a batch of
recipients and records campaign context for each successful send, so the
inbound greeting short-circuit (app/bot/agent.py) can message-match their
first "hi" reply. See app/bot/whatsapp_templates.py for the template
registry and app/api/internal.py for the HTTP entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.whatsapp_templates import ACQUISITION_TEMPLATE_NAMES, send_template_and_record
from app.utils.observability import mask_phone

logger = logging.getLogger("wishkr.campaign_sender")

MAX_RECIPIENTS_PER_REQUEST = 50


@dataclass
class CampaignRecipient:
    phone: str
    name: str | None = None


@dataclass
class CampaignSendResult:
    total: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)


class InvalidCampaignTemplate(ValueError):
    pass


class TooManyRecipients(ValueError):
    pass


def _normalize_phone(phone: str) -> str | None:
    """Return a Meta-legal digit-string phone, or None if malformed.

    Delegates to WhatsAppService._format_phone — the one place in this
    codebase that encodes the 10-digit-IN / 11-digit-legacy-0 / already-
    prefixed-international rules. Duplicating that logic here would risk
    the two copies drifting.
    """
    from app.services.whatsapp import whatsapp_service

    try:
        return whatsapp_service._format_phone(phone)
    except ValueError:
        return None


async def send_acquisition_campaign(
    db: AsyncSession,
    *,
    template_name: str,
    recipients: list[CampaignRecipient],
    campaign_source: str | None = None,
    dry_run: bool = False,
) -> CampaignSendResult:
    """Send ``template_name`` to every recipient and record campaign context.

    Validates ``template_name`` is a recognized acquisition template and
    caps the batch at ``MAX_RECIPIENTS_PER_REQUEST`` before sending
    anything. Duplicate phones (after normalization) are skipped — only
    the first occurrence in ``recipients`` is sent. Malformed phones are
    skipped without attempting a send. Each successful Meta send commits
    its own campaign-context row immediately, so a crash partway through
    a large batch doesn't lose already-sent recipients' context.

    ``dry_run=True`` validates and reports what *would* happen — phone
    normalization, dedup, template validation — without calling Meta or
    writing any CampaignContext rows.
    """
    if template_name not in ACQUISITION_TEMPLATE_NAMES:
        raise InvalidCampaignTemplate(
            f"'{template_name}' is not a recognized acquisition template"
        )
    if len(recipients) > MAX_RECIPIENTS_PER_REQUEST:
        raise TooManyRecipients(
            f"{len(recipients)} recipients exceeds the max of {MAX_RECIPIENTS_PER_REQUEST} per request"
        )

    result = CampaignSendResult(total=len(recipients))
    seen_phones: set[str] = set()

    for recipient in recipients:
        normalized = _normalize_phone(recipient.phone)
        if normalized is None:
            result.skipped += 1
            result.errors.append({
                "phone": recipient.phone,
                "reason": "invalid_phone",
            })
            logger.warning("campaign send skipped: invalid phone (raw=%r)", recipient.phone)
            continue

        if normalized in seen_phones:
            result.skipped += 1
            result.errors.append({
                "phone": normalized,
                "reason": "duplicate_in_request",
            })
            logger.warning("campaign send skipped: duplicate phone=%s", mask_phone(normalized))
            continue
        seen_phones.add(normalized)

        body_params = [recipient.name or "there"]

        if dry_run:
            result.sent += 1
            logger.info(
                "[dry_run] would send template=%s phone=%s", template_name, mask_phone(normalized),
            )
            continue

        try:
            await send_template_and_record(
                db, normalized,
                template_name=template_name,
                body_params=body_params,
                campaign_source=campaign_source,
            )
            await db.commit()
            result.sent += 1
            logger.info(
                "campaign send OK template=%s phone=%s", template_name, mask_phone(normalized),
            )
        except Exception as e:
            await db.rollback()
            result.failed += 1
            result.errors.append({
                "phone": normalized,
                "reason": str(e),
            })
            logger.error(
                "campaign send FAILED template=%s phone=%s err=%s",
                template_name, mask_phone(normalized), e,
            )

    return result
