"""Vendor self-service product editing via a shareable, revocable link.

A vendor opens `/vendor-edit/<token>` (in the admin app) and edits or adds their
own products. Nothing they submit touches the live catalogue — each submission
is stored as a ProductPendingChange for an admin to approve or reject.

Two auth surfaces:
- Vendor endpoints authenticate with the raw link token (hashed + compared
  against vendor_edit_tokens). The token alone scopes every call to one vendor.
- Admin endpoints (mint/revoke link, list/approve/reject pending changes) reuse
  the shared admin token via require_admin_token.
"""

import hashlib
import secrets
from datetime import datetime
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.admin_catalog import (
    AddonPayload,
    VariantPayload,
    require_admin_token,
    sync_product_children,
)
from app.api.catalog import serialize_product
from app.core.config import settings
from app.db.session import get_db
from app.models.product import Product
from app.models.vendor import Vendor
from app.models.vendor_edit import ProductPendingChange, VendorEditToken
from app.utils.html_sanitize import sanitize_html
from app.utils.id_gen import generate_id

# Public router: vendor-facing endpoints, authenticated by the link token alone.
router = APIRouter(prefix="/vendor-edit", tags=["vendor-edit"])

# Admin router: link management + review. Mounted under /admin/vendor-edit so the
# admin app's /api/backend/<path> proxy (which targets /api/admin/<path>) reaches
# it as vendor-edit/... and the shared admin token gates every call.
admin_router = APIRouter(prefix="/admin/vendor-edit", tags=["vendor-edit-admin"])

# Fields a vendor may set on their products. Server-enforced allowlist — anything
# else in a submission is dropped. NEVER includes active/lifecycle/vendor_id, so
# a vendor can't publish their own listing or reassign ownership.
ALLOWED_FIELDS = {
    "name",
    "description",
    "whats_included",
    "care_instructions",
    "customization_schema",
    "product_images",
    "image_url",
    "video_url",
    "base_price_paise",
    "discounted_price_paise",
    "inventory_quantity",
    "dispatch_lead_time_days",
    "delivery_pincodes",
    "ships_nationally",
    "supports_gift_message",
    "preparation_time",
    "category",
    "occasions",
    "variants",
    "addons",
}

# gst_rate / hsn are intentionally NOT vendor-editable: a wrong tax rate or HSN
# code makes Wishkr's invoices/filings wrong. They are shown read-only in the
# editor but never accepted from a vendor submission.

# Subset of ALLOWED_FIELDS that map to child tables, not Product columns. These
# are applied via sync_product_children on approval, never setattr'd.
CHILD_COLLECTION_FIELDS = {"variants", "addons"}


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _vendor_for_token(db: AsyncSession, token: str) -> Vendor:
    """Resolve a raw link token to its (non-revoked) vendor, or 403."""
    if not token:
        raise HTTPException(status_code=403, detail="forbidden")
    row = (
        await db.execute(
            select(VendorEditToken).where(
                VendorEditToken.token_hash == _hash_token(token),
                VendorEditToken.revoked.is_(False),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=403, detail="forbidden")
    vendor = await db.get(Vendor, row.vendor_id)
    if vendor is None:
        raise HTTPException(status_code=403, detail="forbidden")
    return vendor


def _clean_payload(data: dict) -> dict:
    """Keep only allowlisted fields with a non-None value, and sanitise the
    rich-text description (vendor input is untrusted — server is the boundary)."""
    cleaned = {k: v for k, v in data.items() if k in ALLOWED_FIELDS and v is not None}
    if "description" in cleaned and isinstance(cleaned["description"], str):
        cleaned["description"] = sanitize_html(cleaned["description"])
    return cleaned


def _change_bucket_key(product_id: str | None) -> str:
    return product_id or "new"


async def _assert_vendor_product(db: AsyncSession, vendor: Vendor, product_id: str | None) -> None:
    if product_id is None:
        return
    product = await db.get(Product, product_id)
    if product is None or product.vendor_id != vendor.id:
        raise HTTPException(status_code=404, detail="product not found")


async def _find_unreviewed_new_product_change(
    db: AsyncSession, vendor_id: str, draft_id: str
) -> ProductPendingChange | None:
    """Find this vendor's still-`pending` new-product proposal matching
    `draft_id`, if one exists.

    `draft_id` is a client-generated identifier for one "new product"
    proposal, stable across resubmits of that same proposal but distinct per
    "Add product" click. Matching on it (rather than just "vendor + product_id
    is null") is what lets two different proposals from the same vendor stay
    independent while both are pending — without it, a vendor starting a
    second new product before the first is reviewed would silently overwrite
    the first one's payload, since both arrive with product_id=None.

    Once the admin approves or rejects a proposal, this returns None for that
    draft_id: the real product now exists (or the proposal was declined), so
    the *next* submission under that draft_id would be a stale resubmit, not
    a continuation — the caller falls back to creating a fresh row.

    A partial unique index enforces at most one pending row per
    (vendor_id, draft_id), but `.first()` (not `.scalar_one_or_none()`) is
    used defensively so any pre-migration duplicate rows degrade to "pick
    one" instead of a 500.
    """
    return (
        await db.execute(
            select(ProductPendingChange)
            .where(
                ProductPendingChange.vendor_id == vendor_id,
                ProductPendingChange.draft_id == draft_id,
                ProductPendingChange.status == "pending",
            )
            .order_by(ProductPendingChange.submitted_at.desc())
            .limit(1)
        )
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Vendor-facing endpoints (authenticated by the link token)
# --------------------------------------------------------------------------- #


@router.get("/{token}/products")
async def list_my_products(token: str, db: AsyncSession = Depends(get_db)) -> dict:
    vendor = await _vendor_for_token(db, token)
    products = (
        (
            await db.execute(
                select(Product)
                .where(Product.vendor_id == vendor.id)
                .options(
                    selectinload(Product.vendor),
                    selectinload(Product.variants),
                    selectinload(Product.addons),
                )
                .order_by(Product.created_at)
            )
        )
        .scalars()
        .all()
    )
    pending = (
        (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == vendor.id,
                    ProductPendingChange.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    pending_by_product: dict[str, list[dict]] = {}
    for p in pending:
        pending_by_product.setdefault(_change_bucket_key(p.product_id), []).append(
            {
                "id": p.id,
                "draft_id": p.draft_id,
                "payload": p.payload,
                "submitted_at": p.submitted_at.isoformat(),
            }
        )

    drafts = (
        (
            await db.execute(
                select(ProductPendingChange).where(
                    ProductPendingChange.vendor_id == vendor.id,
                    ProductPendingChange.status == "draft",
                )
            )
        )
        .scalars()
        .all()
    )
    drafts_by_product: dict[str, list[dict]] = {}
    for draft in drafts:
        drafts_by_product.setdefault(_change_bucket_key(draft.product_id), []).append(
            {
                "id": draft.id,
                "draft_id": draft.draft_id,
                "payload": draft.payload,
                "submitted_at": draft.submitted_at.isoformat(),
            }
        )

    return {
        "vendor": {"id": vendor.id, "name": vendor.name},
        "products": [serialize_product(p) for p in products],
        "pending_changes": pending_by_product,
        "draft_changes": drafts_by_product,
    }


class PendingSubmission(BaseModel):
    # null -> a brand-new product the vendor is proposing
    product_id: str | None = None
    # Required when product_id is null: a client-generated id identifying this
    # one "new product" proposal (same value across resubmits/autosaves of it,
    # a fresh value per "Add product" click). Lets the backend distinguish
    # "still editing this same proposal" from "a second, distinct new product"
    # when both arrive with product_id=None. Ignored for edits to an existing
    # product_id.
    draft_id: str | None = None
    fields: dict = Field(default_factory=dict)


@router.post("/{token}/submit")
async def submit_change(
    token: str, body: PendingSubmission, db: AsyncSession = Depends(get_db)
) -> dict:
    vendor = await _vendor_for_token(db, token)
    vendor_id = vendor.id

    await _assert_vendor_product(db, vendor, body.product_id)

    payload = _clean_payload(body.fields)
    if not payload:
        raise HTTPException(status_code=422, detail="no editable fields provided")

    if body.product_id is None:
        if not body.draft_id:
            raise HTTPException(
                status_code=422, detail="draft_id is required for a new-product submission"
            )
        # Still-pending proposal from a prior "new" submission with the same
        # draft_id: update it in place so the admin reviews one item with the
        # latest details, rather than a second, separate new-product proposal.
        existing = await _find_unreviewed_new_product_change(db, vendor_id, body.draft_id)
        if existing is not None:
            existing.payload = payload
            change = existing
        else:
            change = ProductPendingChange(
                id=generate_id("ppc"),
                vendor_id=vendor_id,
                product_id=None,
                draft_id=body.draft_id,
                payload=payload,
                status="pending",
            )
            db.add(change)
    else:
        change = ProductPendingChange(
            id=generate_id("ppc"),
            vendor_id=vendor_id,
            product_id=body.product_id,
            payload=payload,
            status="pending",
        )
        db.add(change)

    try:
        # Explicit flush (rather than letting the draft SELECT below autoflush
        # it) so a unique-constraint violation on the pending insert above
        # raises here, inside this try/except, instead of surfacing from the
        # SELECT's implicit autoflush where it wouldn't be caught.
        await db.flush()

        draft_query = select(ProductPendingChange).where(
            ProductPendingChange.vendor_id == vendor_id,
            ProductPendingChange.status == "draft",
        )
        if body.product_id is None:
            draft_query = draft_query.where(ProductPendingChange.draft_id == body.draft_id)
        else:
            draft_query = draft_query.where(ProductPendingChange.product_id == body.product_id)
        draft = (
            await db.execute(draft_query.order_by(ProductPendingChange.submitted_at.desc()).limit(1))
        ).scalars().first()
        if draft is not None:
            await db.delete(draft)

        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if body.product_id is not None:
            raise HTTPException(status_code=409, detail="Could not submit: conflicts with existing data") from exc
        # Lost a race: another request for the same draft_id committed its
        # pending row first (both saw "no existing row" before either
        # inserted). Fall back to updating that row instead of a bare 500.
        existing = await _find_unreviewed_new_product_change(db, vendor_id, body.draft_id)
        if existing is None:
            raise HTTPException(status_code=409, detail="Could not submit: conflicts with existing data") from exc
        existing.payload = payload
        await db.commit()
        return {"status": "pending", "change_id": existing.id}

    return {"status": "pending", "change_id": change.id}


async def _find_existing_draft(
    db: AsyncSession, vendor_id: str, *, product_id: str | None, draft_id: str | None
) -> ProductPendingChange | None:
    draft_query = select(ProductPendingChange).where(
        ProductPendingChange.vendor_id == vendor_id,
        ProductPendingChange.status == "draft",
    )
    if product_id is None:
        draft_query = draft_query.where(ProductPendingChange.draft_id == draft_id)
    else:
        draft_query = draft_query.where(ProductPendingChange.product_id == product_id)
    return (
        await db.execute(draft_query.order_by(ProductPendingChange.submitted_at.desc()).limit(1))
    ).scalars().first()


@router.post("/{token}/save-draft")
async def save_draft(
    token: str, body: PendingSubmission, db: AsyncSession = Depends(get_db)
) -> dict:
    vendor = await _vendor_for_token(db, token)
    vendor_id = vendor.id
    await _assert_vendor_product(db, vendor, body.product_id)

    payload = _clean_payload(body.fields)
    if not payload:
        raise HTTPException(status_code=422, detail="no editable fields provided")

    target_product_id = body.product_id
    if target_product_id is None and not body.draft_id:
        raise HTTPException(
            status_code=422, detail="draft_id is required for a new-product draft"
        )

    draft = await _find_existing_draft(
        db, vendor_id, product_id=target_product_id, draft_id=body.draft_id
    )

    if draft is None:
        draft = ProductPendingChange(
            id=generate_id("ppc"),
            vendor_id=vendor_id,
            product_id=target_product_id,
            draft_id=body.draft_id if target_product_id is None else None,
            payload=payload,
            status="draft",
        )
        db.add(draft)
    else:
        draft.payload = payload

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # Lost a race: another concurrent autosave for the same draft
        # committed first. Fall back to updating that row instead of a 500.
        existing = await _find_existing_draft(
            db, vendor_id, product_id=target_product_id, draft_id=body.draft_id
        )
        if existing is None:
            raise HTTPException(status_code=409, detail="Could not save draft: conflicts with existing data") from exc
        existing.payload = payload
        await db.commit()
        return {"status": "draft", "change_id": existing.id}

    return {"status": "draft", "change_id": draft.id}


class StockToggle(BaseModel):
    product_id: str
    in_stock: bool


@router.post("/{token}/stock")
async def set_stock(
    token: str, body: StockToggle, db: AsyncSession = Depends(get_db)
) -> dict:
    """Instantly set a product in/out of stock — NO admin review. Availability is
    time-sensitive and safe (it only blocks buying), so vendors control it live.
    Ownership is still checked against the link token."""
    vendor = await _vendor_for_token(db, token)
    product = await db.get(Product, body.product_id)
    if product is None or product.vendor_id != vendor.id:
        raise HTTPException(status_code=404, detail="product not found")
    product.in_stock = body.in_stock
    await db.commit()
    return {"product_id": product.id, "in_stock": product.in_stock}


@router.post("/{token}/upload-image")
async def upload_image(
    token: str, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
) -> dict:
    """Vendor image upload — lands in R2 immediately (per spec) and returns the
    public URL for the vendor to attach to a pending change."""
    vendor = await _vendor_for_token(db, token)
    if not all(
        [
            settings.r2_endpoint,
            settings.r2_bucket,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_public_base_url,
        ]
    ):
        raise HTTPException(status_code=503, detail="R2 is not configured")
    try:
        import boto3
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="boto3 is not installed") from exc

    suffix = PurePosixPath(file.filename or "image").suffix.lower()[:12] or ".jpg"
    key = f"vendor-uploads/{vendor.id}/{uuid4().hex}{suffix}"
    body = await file.read()
    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )
    client.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=body,
        ContentType=file.content_type or "application/octet-stream",
    )
    public_url = f"{settings.r2_public_base_url.rstrip('/')}/{key}"
    return {"image_url": public_url}


@router.get("/{token}/preview/{product_id}")
async def preview_with_draft(
    token: str, product_id: str, db: AsyncSession = Depends(get_db), draft_id: str | None = None
) -> dict:
    """Return the vendor's product serialised for the storefront PDP, with their
    saved draft merged on top — so the vendor previews exactly what buyers would
    see, including unsubmitted edits. `product_id` may be "new" for a draft of a
    not-yet-created product, in which case `draft_id` identifies which proposal
    (a vendor can have more than one new-product draft in flight). Authorised by
    the link token + ownership check."""
    vendor = await _vendor_for_token(db, token)

    is_new = product_id == "new"
    if not is_new:
        await _assert_vendor_product(db, vendor, product_id)
        product = (
            await db.execute(
                select(Product)
                .where(Product.id == product_id)
                .options(
                    selectinload(Product.vendor),
                    selectinload(Product.variants),
                    selectinload(Product.addons),
                )
            )
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
    elif not draft_id:
        raise HTTPException(status_code=422, detail="draft_id is required to preview a new product")

    draft_query = select(ProductPendingChange).where(
        ProductPendingChange.vendor_id == vendor.id,
        ProductPendingChange.status == "draft",
    )
    if is_new:
        draft_query = draft_query.where(ProductPendingChange.draft_id == draft_id)
    else:
        draft_query = draft_query.where(ProductPendingChange.product_id == product_id)
    draft = (
        await db.execute(draft_query.order_by(ProductPendingChange.submitted_at.desc()).limit(1))
    ).scalars().first()
    overlay = _clean_payload((draft.payload if draft else {}) or {})

    # variants/addons live in child tables, not Product columns — setattr'ing
    # the draft's raw dicts onto product.variants/addons breaks the ORM
    # collection machinery (it expects ProductVariant/ProductAddon instances,
    # not dicts) and would cascade transient rows into the session as pending
    # inserts. Serialise them separately, mirroring serialize_product's shape.
    scalars = {k: v for k, v in overlay.items() if k not in CHILD_COLLECTION_FIELDS}
    try:
        draft_variants = [
            VariantPayload(**v) for v in overlay.get("variants", []) if isinstance(v, dict)
        ]
        draft_addons = [
            AddonPayload(**a) for a in overlay.get("addons", []) if isinstance(a, dict)
        ]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid variant/add-on: {exc}") from exc

    def _serialise_draft_children() -> tuple[list[dict], list[dict]]:
        variants = [
            {
                "id": v.id or "preview",
                "name": v.name,
                "sku": v.sku,
                "price_delta_paise": v.price_delta_paise,
                "active": v.active,
                "sort_order": v.sort_order or index + 1,
            }
            for index, v in enumerate(draft_variants)
            if v.active
        ]
        addons = [
            {
                "id": a.id or "preview",
                "name": a.name,
                "price_paise": a.price_paise,
                "active": a.active,
                "sort_order": a.sort_order or index + 1,
                "allow_quantity": a.allow_quantity,
                "min_quantity": a.min_quantity,
                "max_quantity": a.max_quantity,
            }
            for index, a in enumerate(draft_addons)
            if a.active
        ]
        return variants, addons

    if is_new:
        # Synthesise a transient product from the draft so the PDP can render it.
        # Not added to the session — purely for serialisation.
        product = Product(
            id="new",
            vendor_id=vendor.id,
            name=scalars.get("name") or "Untitled product",
            base_price_paise=int(scalars.get("base_price_paise") or 0),
            active=False,
            lifecycle_status="draft",
            variants=[],
            addons=[],
        )
        product.vendor = vendor
        for key, value in scalars.items():
            setattr(product, key, value)
        serialised = serialize_product(product)
        serialised["variants"], serialised["addons"] = _serialise_draft_children()
        return {"product": serialised}

    # Merge the draft onto the live product (in memory only — never committed).
    for key, value in scalars.items():
        setattr(product, key, value)
    serialised = serialize_product(product)
    if "variants" in overlay or "addons" in overlay:
        variants, addons = _serialise_draft_children()
        if "variants" in overlay:
            serialised["variants"] = variants
        if "addons" in overlay:
            serialised["addons"] = addons
    # Don't leave the session object mutated/flushed accidentally.
    await db.rollback()
    return {"product": serialised}


# --------------------------------------------------------------------------- #
# Admin-facing endpoints (mint/revoke link, review pending changes)
# --------------------------------------------------------------------------- #


@admin_router.post("/vendors/{vendor_id}/link", dependencies=[Depends(require_admin_token)])
async def mint_link(vendor_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Mint a fresh link token for a vendor, revoking any previous one. The raw
    token is returned ONCE — only its hash is stored."""
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="vendor not found")

    existing = (
        (
            await db.execute(
                select(VendorEditToken).where(
                    VendorEditToken.vendor_id == vendor_id,
                    VendorEditToken.revoked.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        row.revoked = True

    raw = secrets.token_urlsafe(32)
    token_row = VendorEditToken(
        id=generate_id("vet"), vendor_id=vendor_id, token_hash=_hash_token(raw)
    )
    db.add(token_row)
    await db.commit()
    return {"token": raw, "vendor_id": vendor_id}


@admin_router.delete("/vendors/{vendor_id}/link", dependencies=[Depends(require_admin_token)])
async def revoke_link(vendor_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        (
            await db.execute(
                select(VendorEditToken).where(
                    VendorEditToken.vendor_id == vendor_id,
                    VendorEditToken.revoked.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked = True
    await db.commit()
    return {"revoked": len(rows)}


@admin_router.get("/pending", dependencies=[Depends(require_admin_token)])
async def list_pending(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        (
            await db.execute(
                select(ProductPendingChange)
                .where(ProductPendingChange.status == "pending")
                .order_by(ProductPendingChange.submitted_at)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for r in rows:
        product = await db.get(Product, r.product_id) if r.product_id else None
        vendor = await db.get(Vendor, r.vendor_id)
        out.append(
            {
                "id": r.id,
                "vendor_id": r.vendor_id,
                "vendor_name": vendor.name if vendor else None,
                "product_id": r.product_id,
                "product_name": product.name if product else None,
                "is_new_product": r.product_id is None,
                "payload": r.payload,
                "submitted_at": r.submitted_at.isoformat(),
            }
        )
    return {"pending": out}


@admin_router.patch("/pending/{change_id}", dependencies=[Depends(require_admin_token)])
async def edit_pending_change(
    change_id: str, body: PendingSubmission, db: AsyncSession = Depends(get_db)
) -> dict:
    """Let an admin correct a vendor's proposed change before approving it (e.g.
    fix a typo or price) instead of only being able to approve or reject the
    submission verbatim. Same allowlist/sanitisation as a vendor submission —
    the admin can't add fields a vendor couldn't have proposed."""
    change = await db.get(ProductPendingChange, change_id)
    if change is None or change.status != "pending":
        raise HTTPException(status_code=404, detail="pending change not found")

    payload = _clean_payload(body.fields)
    if not payload:
        raise HTTPException(status_code=422, detail="no editable fields provided")

    change.payload = payload
    await db.commit()
    return {"status": "pending", "change_id": change.id, "payload": change.payload}


@admin_router.post("/pending/{change_id}/approve", dependencies=[Depends(require_admin_token)])
async def approve_change(change_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    change = await db.get(ProductPendingChange, change_id)
    if change is None or change.status != "pending":
        raise HTTPException(status_code=404, detail="pending change not found")

    payload = _clean_payload(change.payload or {})
    # Split scalar columns (setattr-able) from child collections (variants/addons
    # live in their own tables and must go through sync_product_children).
    scalars = {k: v for k, v in payload.items() if k not in CHILD_COLLECTION_FIELDS}
    try:
        variants = [VariantPayload(**v) for v in payload.get("variants", []) if isinstance(v, dict)]
        addons = [AddonPayload(**a) for a in payload.get("addons", []) if isinstance(a, dict)]
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid variant/add-on: {exc}") from exc

    if change.product_id is None:
        # New product proposed by the vendor — create it as a draft (admin still
        # controls going live via lifecycle/active).
        product = Product(
            id=generate_id("prd"),
            vendor_id=change.vendor_id,
            name=scalars.get("name") or "Untitled product",
            base_price_paise=int(scalars.get("base_price_paise") or 0),
            active=False,
            lifecycle_status="draft",
            variants=[],
            addons=[],
        )
        for k, v in scalars.items():
            setattr(product, k, v)
        db.add(product)
        await db.flush()  # assign product.id before child rows reference it
        # Link this change to the product it created so any later "new"-editor
        # submission from the vendor folds into an edit, not a second create.
        change.created_product_id = product.id
        change.product_id = product.id
    else:
        product = (
            await db.execute(
                select(Product)
                .where(Product.id == change.product_id)
                .options(selectinload(Product.variants), selectinload(Product.addons))
            )
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=404, detail="product no longer exists")
        for k, v in scalars.items():
            setattr(product, k, v)

    # Only reconcile a collection when the vendor actually submitted it, so an
    # edit that includes only one of variants/addons never wipes the other.
    if "variants" in payload or "addons" in payload:
        await sync_product_children(
            db,
            product,
            variants,
            addons,
            sync_variants="variants" in payload,
            sync_addons="addons" in payload,
        )

    change.status = "approved"
    change.reviewed_at = datetime.utcnow()
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Could not approve: this change conflicts with existing data ({exc.orig})",
        ) from exc
    return {"status": "approved", "product_id": product.id}


@admin_router.post("/pending/{change_id}/reject", dependencies=[Depends(require_admin_token)])
async def reject_change(change_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    change = await db.get(ProductPendingChange, change_id)
    if change is None or change.status != "pending":
        raise HTTPException(status_code=404, detail="pending change not found")
    change.status = "rejected"
    change.reviewed_at = datetime.utcnow()
    await db.commit()
    return {"status": "rejected"}
