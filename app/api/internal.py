"""Internal/admin endpoints for ops observability."""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.utils.cache_hit_monitor import check_cache_hit_efficiency
from app.utils.llm_spend_report import get_llm_spend_report
from app.bot.whatsapp_templates import ACQUISITION_TEMPLATE_NAMES, record_campaign_context
from app.services.campaign_sender import (
    CampaignRecipient,
    InvalidCampaignTemplate,
    TooManyRecipients,
    send_acquisition_campaign,
)

router = APIRouter(prefix="/internal", tags=["internal"])


def _require_internal_token(x_internal_token: str | None) -> None:
    expected = settings.admin_api_token
    if not expected or not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/llm/spend")
async def llm_spend_report(
    db: Annotated[AsyncSession, Depends(get_db)],
    x_internal_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Return spend + cache efficiency report for internal ops."""
    _require_internal_token(x_internal_token)
    report = await get_llm_spend_report(db)
    report["cache_monitor"] = await check_cache_hit_efficiency(db)
    return report


class CampaignContextIn(BaseModel):
    phone: str
    template_name: str
    campaign_source: str | None = None


@router.post("/campaign-context")
async def record_campaign_context_endpoint(
    payload: CampaignContextIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_internal_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Record which acquisition template a phone was just sent.

    Call this immediately after sending a cold-acquisition WhatsApp
    template (any name in ``ACQUISITION_TEMPLATE_NAMES``) from a tool
    that lives outside this backend — a campaign blast, Meta Business
    Manager automation, or similar. The inbound greeting short-circuit
    (``app/bot/agent.py``) reads this back on the phone's first "hi"
    reply to message-match the welcome copy to the template's hook.

    Rejects template names not in ``ACQUISITION_TEMPLATE_NAMES`` — this
    endpoint is only for first-touch/welcome campaigns, not transactional
    or reminder sends, which never read from CampaignContext.
    """
    _require_internal_token(x_internal_token)
    if payload.template_name not in ACQUISITION_TEMPLATE_NAMES:
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.template_name}' is not a recognized acquisition template",
        )
    await record_campaign_context(
        db, payload.phone,
        template_name=payload.template_name,
        campaign_source=payload.campaign_source,
    )
    await db.commit()
    return {"success": True}


class CampaignRecipientIn(BaseModel):
    phone: str
    name: str | None = None


class SendAcquisitionCampaignIn(BaseModel):
    template_name: str
    campaign_source: str | None = None
    recipients: list[CampaignRecipientIn]
    dry_run: bool = False


@router.post("/send-acquisition-campaign")
async def send_acquisition_campaign_endpoint(
    payload: SendAcquisitionCampaignIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_internal_token: Annotated[str | None, Header()] = None,
) -> dict:
    """Send an acquisition/welcome WhatsApp template to a batch of recipients.

    Sends via Meta and records ``CampaignContext`` for each successful
    send (see ``app.services.campaign_sender``), so the inbound greeting
    short-circuit can message-match the recipient's first "hi" reply to
    this specific template. ``template_name`` must be a recognized
    acquisition template (rejects transactional/reminder templates).
    Max 50 recipients per request; duplicate phones (post-normalization)
    and malformed phones are skipped, not sent. Pass ``dry_run: true`` to
    validate/preview without calling Meta or writing any DB rows.
    """
    _require_internal_token(x_internal_token)
    try:
        result = await send_acquisition_campaign(
            db,
            template_name=payload.template_name,
            recipients=[
                CampaignRecipient(phone=r.phone, name=r.name) for r in payload.recipients
            ],
            campaign_source=payload.campaign_source,
            dry_run=payload.dry_run,
        )
    except InvalidCampaignTemplate as e:
        raise HTTPException(status_code=422, detail=str(e))
    except TooManyRecipients as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "total": result.total,
        "sent": result.sent,
        "failed": result.failed,
        "skipped": result.skipped,
        "errors": result.errors,
    }
