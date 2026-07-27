"""product_pending_changes draft-status uniqueness

save_draft's check-then-insert had no DB-level uniqueness backing it (unlike
submit_change, which the prior migration protected for status='pending').
Two concurrent autosaves for the same (vendor, draft_id) or (vendor,
product_id) could both observe "no draft row yet" and both insert, leaving
duplicate draft rows — submit_change's cleanup only deletes one (LIMIT 1),
orphaning the rest.

NULL never equals NULL under a unique index, so a single index keyed on
draft_id (new-product drafts; product_id is always NULL there) cannot also
police product_id-keyed edit drafts (draft_id is always NULL there) — hence
two separate partial indexes, each scoped to the rows where its key column
is populated.

Revision ID: c7e1f92b4a06
Revises: b3c8f61a9d24
Create Date: 2026-07-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7e1f92b4a06"
down_revision: Union[str, None] = "b3c8f61a9d24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive dedup: if this exact race already produced duplicate draft
    # rows in production, keep only the most recently saved one per key so
    # the unique index below can actually be created.
    op.execute(
        """
        DELETE FROM product_pending_changes
        WHERE status = 'draft' AND draft_id IS NOT NULL AND id NOT IN (
            SELECT DISTINCT ON (vendor_id, draft_id) id
            FROM product_pending_changes
            WHERE status = 'draft' AND draft_id IS NOT NULL
            ORDER BY vendor_id, draft_id, submitted_at DESC
        )
        """
    )
    op.execute(
        """
        DELETE FROM product_pending_changes
        WHERE status = 'draft' AND product_id IS NOT NULL AND id NOT IN (
            SELECT DISTINCT ON (vendor_id, product_id) id
            FROM product_pending_changes
            WHERE status = 'draft' AND product_id IS NOT NULL
            ORDER BY vendor_id, product_id, submitted_at DESC
        )
        """
    )
    op.create_index(
        "ux_product_pending_changes_vendor_draft_id_draft",
        "product_pending_changes",
        ["vendor_id", "draft_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft' AND draft_id IS NOT NULL"),
    )
    op.create_index(
        "ux_product_pending_changes_vendor_product_draft",
        "product_pending_changes",
        ["vendor_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft' AND product_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_product_pending_changes_vendor_product_draft",
        table_name="product_pending_changes",
    )
    op.drop_index(
        "ux_product_pending_changes_vendor_draft_id_draft",
        table_name="product_pending_changes",
    )
