"""Automatic audit logging for admin write requests.

Every successful mutating call (POST/PATCH/PUT/DELETE) under ``/api/admin/`` is
recorded to ``audit_logs`` with the acting user's email (from the
``X-Admin-Email`` header the admin web proxy forwards), so we can answer
"which user did what". Reads (GET/HEAD) are never logged.

This complements the hand-written ``record_audit`` calls in a few endpoints —
those add nicer human summaries; this guarantees blanket coverage with a
generic summary, and any new admin route is covered automatically.
"""

from starlette.requests import Request
from starlette.responses import Response

from app.db.session import async_session
from app.models.admin_user import AuditLog
from app.utils.id_gen import generate_id

_AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_ADMIN_PREFIX = "/api/admin/"

# AuditLog column limits (see app/models/admin_user.py).
_ACTION_MAX = 80
_ENTITY_ID_MAX = 40
_SUMMARY_MAX = 500


def _looks_like_id(segment: str) -> bool:
    # Our ids are "<prefix>_<ts>_<rand>" (see utils.id_gen), e.g. "ven_..." —
    # slugs and static path words never contain that shape.
    return "_" in segment and any(ch.isdigit() for ch in segment)


def _derive(path: str) -> tuple[str, str | None]:
    """Return (action, entity_id) from an admin path.

    The first id-shaped segment becomes ``entity_id``; id segments are dropped
    from the action so it stays a stable, readable resource path:

        "/api/admin/catalog/vendors/ven_123"               -> ("catalog/vendors", "ven_123")
        "/api/admin/catalog/vendors/ven_123/documents/pan" -> ("catalog/vendors/documents/pan", "ven_123")
    """
    rest = path[len(_ADMIN_PREFIX):]
    segments = [s for s in rest.split("/") if s]
    entity_id = None
    kept = []
    for seg in segments:
        if _looks_like_id(seg):
            if entity_id is None:
                entity_id = seg[:_ENTITY_ID_MAX]
            continue  # drop all id-shaped segments from the action
        kept.append(seg)
    action = "/".join(kept)[:_ACTION_MAX] or "admin"
    return action, entity_id


async def audit_admin_writes(request: Request, call_next):
    response: Response = await call_next(request)

    if request.method not in _AUDITED_METHODS:
        return response
    if not request.url.path.startswith(_ADMIN_PREFIX):
        return response
    # Only log actions that actually succeeded — not 4xx/5xx attempts.
    if response.status_code >= 400:
        return response

    actor = (request.headers.get("x-admin-email") or "").strip() or "system"
    action, entity_id = _derive(request.url.path)
    summary = f"{request.method} {request.url.path}"[:_SUMMARY_MAX]

    # Best-effort: a failed audit write must never break the actual request.
    try:
        async with async_session() as session:
            session.add(
                AuditLog(
                    id=generate_id("aud"),
                    actor=actor,
                    action=action,
                    entity_type=None,
                    entity_id=entity_id,
                    summary=summary,
                )
            )
            await session.commit()
    except Exception:
        pass

    return response
