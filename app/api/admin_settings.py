"""Admin settings — users & roles, integrations health, notification templates,
audit log. Phase 4 of the admin panel. Guarded by the admin token + role seam.

Auth model: the backend owns admin users. The admin web app calls
POST /api/admin/settings/auth/login (email+password) and, on success, stores a
signed cookie with the user's email+role. The shared X-Admin-Token still guards
all admin routes server-to-server. An env owner (WISHKR_ADMIN_PASSWORD) remains a
fallback so you can never be locked out.
"""

import hashlib
import hmac
import os
from datetime import datetime

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_catalog import require_admin_token, require_role
from app.core.config import settings
from app.db.session import get_db
from app.models.admin_user import AdminUser, AuditLog, NotificationTemplate, PricingSetting
from app.utils.id_gen import generate_id

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])

ROLES = ["owner", "ops", "vendor_success", "blog_editor"]
DEFAULT_VENDOR_TEMPLATE_KEY = "vendor_order_notification"


_PBKDF2_ROUNDS = 200_000


def _hash_password(raw: str) -> str:
    """PBKDF2-HMAC-SHA256 (stdlib only — no external crypto dependency).

    Stored as: pbkdf2$<rounds>$<salt_hex>$<hash_hex>
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _verify_password(raw: str, hashed: str) -> bool:
    try:
        scheme, rounds_s, salt_hex, hash_hex = hashed.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", raw.encode(), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


async def record_audit(
    db: AsyncSession,
    *,
    actor: str | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    summary: str | None = None,
) -> None:
    """Best-effort audit write. Callers commit as part of their own transaction."""
    db.add(
        AuditLog(
            id=generate_id("aud"),
            actor=actor or "system",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
        )
    )


# ── Auth (login by email) ─────────────────────────────────────────────────────

class LoginPayload(BaseModel):
    email: str
    password: str


@router.post("/auth/login", dependencies=[Depends(require_admin_token)])
async def login(payload: LoginPayload, db: AsyncSession = Depends(get_db)) -> dict:
    # Env owner fallback: lets the founder in even before any user row exists.
    env_email = (settings.admin_owner_email or "").strip().lower()
    env_password = settings.admin_api_password_fallback or ""
    if env_email and env_password and payload.email.strip().lower() == env_email:
        if hmac.compare_digest(payload.password, env_password):
            return {"email": env_email, "name": "Owner", "role": "owner"}
        raise HTTPException(status_code=401, detail="invalid credentials")

    user = (
        await db.execute(select(AdminUser).where(AdminUser.email == payload.email.strip().lower()))
    ).scalar_one_or_none()
    if user is None or not user.active or not _verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    user.last_login_at = datetime.utcnow()
    await db.commit()
    return {"email": user.email, "name": user.name, "role": user.role}


# ── Users CRUD (owner only) ───────────────────────────────────────────────────

def serialize_user(u: AdminUser) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "active": u.active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


class UserCreatePayload(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str | None = None
    password: str = Field(min_length=8)
    role: str = "ops"


class UserUpdatePayload(BaseModel):
    name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8)


@router.get("/users", dependencies=[Depends(require_role("owner"))])
async def list_users(db: AsyncSession = Depends(get_db)) -> dict:
    users = (await db.execute(select(AdminUser).order_by(AdminUser.created_at))).scalars().all()
    return {"users": [serialize_user(u) for u in users], "roles": ROLES}


@router.post("/users", dependencies=[Depends(require_role("owner"))])
async def create_user(payload: UserCreatePayload, db: AsyncSession = Depends(get_db)) -> dict:
    if payload.role not in ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    email = payload.email.lower()
    exists = (
        await db.execute(select(AdminUser).where(AdminUser.email == email))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="email already exists")
    user = AdminUser(
        id=generate_id("adm"),
        email=email,
        name=payload.name,
        password_hash=_hash_password(payload.password),
        role=payload.role,
        active=True,
    )
    db.add(user)
    await record_audit(db, actor=None, action="admin_user.create", entity_type="admin_user", entity_id=user.id, summary=f"created {email} ({payload.role})")
    await db.commit()
    return {"user": serialize_user(user)}


@router.patch("/users/{user_id}", dependencies=[Depends(require_role("owner"))])
async def update_user(user_id: str, payload: UserUpdatePayload, db: AsyncSession = Depends(get_db)) -> dict:
    user = await db.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if payload.role is not None:
        if payload.role not in ROLES:
            raise HTTPException(status_code=400, detail="invalid role")
        user.role = payload.role
    if payload.name is not None:
        user.name = payload.name
    if payload.active is not None:
        user.active = payload.active
    if payload.password is not None:
        user.password_hash = _hash_password(payload.password)
    await record_audit(db, actor=None, action="admin_user.update", entity_type="admin_user", entity_id=user.id, summary=f"updated {user.email}")
    await db.commit()
    return {"user": serialize_user(user)}


class ChangePasswordPayload(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


@router.post("/auth/change-password", dependencies=[Depends(require_admin_token)])
async def change_password(
    payload: ChangePasswordPayload,
    db: AsyncSession = Depends(get_db),
    x_admin_email: Annotated[str | None, Header()] = None,
) -> dict:
    """Self-service: the logged-in admin changes their own password.

    Identified by the proxy-forwarded X-Admin-Email header; requires the
    current password. The env-fallback owner has no DB row and can't use this.
    """
    if not x_admin_email:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = (
        await db.execute(
            select(AdminUser).where(AdminUser.email == x_admin_email.strip().lower())
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="account not found")
    if not _verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    user.password_hash = _hash_password(payload.new_password)
    await record_audit(
        db, actor=user.email, action="admin_user.change_password",
        entity_type="admin_user", entity_id=user.id, summary="self password change",
    )
    await db.commit()
    return {"success": True}


# ── Integrations health (read-only) ───────────────────────────────────────────

@router.get("/health", dependencies=[Depends(require_role("owner", "ops"))])
async def integrations_health() -> dict:
    def ok(*vals: str) -> bool:
        return all(bool(v) for v in vals)

    razorpay_mode = getattr(settings, "razorpay_mode", None) or (
        "test" if "test" in (getattr(settings, "razorpay_key_id", "") or "").lower() else "live"
    )
    return {
        "r2": {
            "configured": ok(
                settings.r2_endpoint,
                settings.r2_bucket,
                settings.r2_access_key_id,
                settings.r2_secret_access_key,
                settings.r2_public_base_url,
            ),
            "public_base_url": settings.r2_public_base_url or None,
        },
        "razorpay": {
            "configured": ok(getattr(settings, "razorpay_key_id", "")),
            "mode": razorpay_mode,
        },
        "whatsapp": {
            "configured": ok(
                getattr(settings, "whatsapp_access_token", ""),
                getattr(settings, "whatsapp_phone_number_id", ""),
            ),
        },
        "sentry": {
            "configured": ok(getattr(settings, "sentry_dsn", "")),
        },
    }


# ── Notification templates ────────────────────────────────────────────────────

class TemplatePayload(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@router.get("/templates", dependencies=[Depends(require_role("owner", "ops"))])
async def list_templates(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(NotificationTemplate))).scalars().all()
    return {"templates": [{"key": r.key, "body": r.body, "updated_at": r.updated_at.isoformat() if r.updated_at else None} for r in rows]}


@router.put("/templates/{key}", dependencies=[Depends(require_role("owner", "ops"))])
async def upsert_template(key: str, payload: TemplatePayload, db: AsyncSession = Depends(get_db)) -> dict:
    tmpl = await db.get(NotificationTemplate, key)
    if tmpl is None:
        tmpl = NotificationTemplate(key=key, body=payload.body)
        db.add(tmpl)
    else:
        tmpl.body = payload.body
    await record_audit(db, actor=None, action="template.update", entity_type="template", entity_id=key, summary=f"edited {key}")
    await db.commit()
    return {"template": {"key": tmpl.key, "body": tmpl.body}}


# ── Storefront pricing (markup + flash) ───────────────────────────────────────

class PricingPayload(BaseModel):
    # Upper bounds match NUMERIC(5,2): max 999.99. flash_off < 100 so the charge
    # can never reach ₹0.
    markup_pct: float = Field(ge=0, le=999.99)
    flash_off_pct: float = Field(ge=0, lt=100)
    flash_active: bool

    @model_validator(mode="after")
    def _charge_covers_base_price(self) -> "PricingPayload":
        # An active flash derives the charge from the product base price. Keep
        # its combined multiplier at or above 1.0 so a campaign cannot sell
        # below that reference amount. Settings are neutral while flash is off.
        if self.flash_active:
            charge_mult = (1 + self.markup_pct / 100) * (1 - self.flash_off_pct / 100)
            if charge_mult < 1.0:
                raise ValueError(
                    "flash discount exceeds markup: the charge would fall below "
                    "the base price. Raise markup_pct or lower flash_off_pct."
                )
        return self


def _serialize_pricing(row: PricingSetting) -> dict:
    return {
        "markup_pct": float(row.markup_pct),
        "flash_off_pct": float(row.flash_off_pct),
        "flash_active": row.flash_active,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/pricing", dependencies=[Depends(require_role("owner", "ops"))])
async def get_pricing(db: AsyncSession = Depends(get_db)) -> dict:
    row = (await db.execute(select(PricingSetting).limit(1))).scalar_one_or_none()
    if row is None:
        return {"pricing": {"markup_pct": 0.0, "flash_off_pct": 0.0, "flash_active": False, "updated_at": None}}
    return {"pricing": _serialize_pricing(row)}


@router.put("/pricing", dependencies=[Depends(require_role("owner"))])
async def update_pricing(payload: PricingPayload, db: AsyncSession = Depends(get_db)) -> dict:
    row = (await db.execute(select(PricingSetting).limit(1))).scalar_one_or_none()
    if row is None:
        row = PricingSetting(id=1)
        db.add(row)
    row.markup_pct = payload.markup_pct
    row.flash_off_pct = payload.flash_off_pct
    row.flash_active = payload.flash_active
    await record_audit(
        db, actor=None, action="pricing.update", entity_type="pricing", entity_id="1",
        summary=f"markup={payload.markup_pct}% flash={payload.flash_off_pct}% active={payload.flash_active}",
    )
    await db.commit()
    # commit() expires the instance; updated_at (onupdate=func.now()) would then
    # lazy-load on access outside greenlet context and raise MissingGreenlet.
    # Refresh eagerly so serialization touches only loaded attributes.
    await db.refresh(row)
    return {"pricing": _serialize_pricing(row)}


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.get("/audit", dependencies=[Depends(require_role("owner", "ops"))])
async def list_audit(db: AsyncSession = Depends(get_db), limit: int = 200) -> dict:
    rows = (
        await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500)))
    ).scalars().all()
    return {
        "events": [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "summary": r.summary,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
