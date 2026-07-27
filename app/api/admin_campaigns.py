"""Admin campaigns — send cold-acquisition WhatsApp templates from the admin UI.

Thin admin-auth wrapper around ``app.services.campaign_sender``, the same
service the standalone ``send_campaign.py`` ops script calls over
``/internal/send-acquisition-campaign``. Reuses its phone normalization,
dedup, and ``CampaignContext`` recording so a number already invited via
either path shows up consistently in ``/admin/campaigns/history``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import require_role
from app.bot.whatsapp_templates import ACQUISITION_TEMPLATE_NAMES
from app.db.session import get_db
from app.models.campaign_context import CampaignContext
from app.services.campaign_sender import (
    CampaignRecipient,
    InvalidCampaignTemplate,
    TooManyRecipients,
    send_acquisition_campaign,
)

router = APIRouter(prefix="/admin/campaigns", tags=["admin-campaigns"])


@router.get("/templates", dependencies=[Depends(require_role("owner", "ops"))])
async def list_campaign_templates() -> dict:
    return {"templates": sorted(ACQUISITION_TEMPLATE_NAMES)}


class CampaignRecipientIn(BaseModel):
    phone: str
    name: str | None = None


class SendCampaignIn(BaseModel):
    template_name: str
    campaign_source: str | None = None
    recipients: list[CampaignRecipientIn]
    dry_run: bool = False


@router.post("/send", dependencies=[Depends(require_role("owner", "ops"))])
async def send_campaign(payload: SendCampaignIn, db: AsyncSession = Depends(get_db)) -> dict:
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


@router.get("/history", dependencies=[Depends(require_role("owner", "ops"))])
async def list_campaign_history(
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    base = select(CampaignContext)
    if q:
        base = base.where(CampaignContext.phone.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    stmt = base.order_by(CampaignContext.sent_at.desc()).limit(limit).offset((page - 1) * limit)
    rows = (await db.execute(stmt)).scalars().all()

    return {
        "history": [
            {
                "phone": c.phone,
                "template_name": c.template_name,
                "campaign_source": c.campaign_source,
                "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                "consumed_at": c.consumed_at.isoformat() if c.consumed_at else None,
            }
            for c in rows
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }
