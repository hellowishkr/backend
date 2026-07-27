from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# A long-lived, revocable token that lets a vendor self-serve edits to their
# own products via a shareable link. The raw token is shown once when minted;
# only its SHA-256 hash is stored, so a DB leak can't reconstruct live links.
# One active token per vendor (minting a new one revokes the old).
class VendorEditToken(Base):
    __tablename__ = "vendor_edit_tokens"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# A change proposed by a vendor through their edit link, held for admin review.
# The live product is never mutated by the vendor — an approval applies the
# payload, a rejection discards it. product_id is null for a brand-new product.
class ProductPendingChange(Base):
    __tablename__ = "product_pending_changes"
    __table_args__ = (
        # At most one still-pending proposal per (vendor, draft_id). Declared
        # here (not just in the alembic migration) so the SQLite engine used
        # by tests enforces the same constraint as production Postgres —
        # otherwise a race between two concurrent submissions for the same
        # draft_id could insert two pending rows unnoticed in tests. Matches
        # alembic/versions/b3c8f61a9d24_pending_change_draft_id.py exactly;
        # keep both in sync if this ever changes.
        Index(
            "ux_product_pending_changes_vendor_draft_pending",
            "vendor_id",
            "draft_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # Draft-status equivalents of the above. NULL never equals NULL under
        # a unique index, so one index keyed on draft_id (new-product drafts,
        # product_id always NULL) can't also police product_id-keyed edit
        # drafts (draft_id always NULL there) — hence two separate partial
        # indexes, each scoped to the rows where its key column is populated.
        # Matches alembic/versions/c7e1f92b4a06_draft_status_uniqueness.py;
        # keep both in sync if this ever changes.
        Index(
            "ux_product_pending_changes_vendor_draft_id_draft",
            "vendor_id",
            "draft_id",
            unique=True,
            postgresql_where=text("status = 'draft' AND draft_id IS NOT NULL"),
            sqlite_where=text("status = 'draft' AND draft_id IS NOT NULL"),
        ),
        Index(
            "ux_product_pending_changes_vendor_product_draft",
            "vendor_id",
            "product_id",
            unique=True,
            postgresql_where=text("status = 'draft' AND product_id IS NOT NULL"),
            sqlite_where=text("status = 'draft' AND product_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    vendor_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("vendors.id", ondelete="CASCADE"), index=True
    )
    # Null = a new product the vendor is proposing; otherwise an edit to this one.
    product_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    # Client-generated identity for a single "new product" proposal, stable
    # across resubmits/autosaves of that same proposal (a fresh value is
    # generated whenever the vendor explicitly starts another "Add product"
    # flow). Distinguishes "still editing the same not-yet-reviewed proposal"
    # from "a second, distinct new product" — both arrive with product_id
    # NULL, so product_id alone can't tell them apart. A partial unique index
    # enforces at most one pending row per (vendor_id, draft_id).
    draft_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Set only when approving a new-product change (product_id was null):
    # records the product that approval created, as an audit trail of which
    # proposal created which product.
    created_product_id: Mapped[str | None] = mapped_column(
        String(30), ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    # The proposed field values (allowlisted subset of Product columns).
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
