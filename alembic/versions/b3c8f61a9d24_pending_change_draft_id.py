"""product_pending_changes.draft_id

Distinct new-product proposals were previously matched only by
(vendor_id, product_id IS NULL, status='pending'), so the backend couldn't
tell "vendor resubmitting the same still-pending proposal" apart from
"vendor started a second, distinct new product before the first was
reviewed" — the second submission silently overwrote the first one's
payload. It could also crash with MultipleResultsFound if more than one
pending product_id-null row ever existed for a vendor (which historical data
already has).

draft_id is a client-generated identifier (new one per "Add product" click,
same one across resubmits/autosaves of that same proposal) that gives each
proposal a stable identity independent of product_id. A partial unique index
enforces at most one pending row per (vendor_id, draft_id) at the DB level.

Revision ID: b3c8f61a9d24
Revises: 466634165c0a
Create Date: 2026-07-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c8f61a9d24"
down_revision: Union[str, None] = "466634165c0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "product_pending_changes",
        sa.Column("draft_id", sa.String(length=40), nullable=True),
    )
    # Backfill: give every existing row its own distinct draft_id derived from
    # its primary key, so historical duplicate pending rows (same vendor,
    # product_id IS NULL) don't collide under the new unique index below.
    op.execute(
        "UPDATE product_pending_changes SET draft_id = id WHERE draft_id IS NULL"
    )
    op.create_index(
        "ux_product_pending_changes_vendor_draft_pending",
        "product_pending_changes",
        ["vendor_id", "draft_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_product_pending_changes_vendor_draft_pending",
        table_name="product_pending_changes",
    )
    op.drop_column("product_pending_changes", "draft_id")
