"""vendor_edit_tokens + product_pending_changes (vendor self-service editing)

Revision ID: a8f3d21b9c47
Revises: e7b2c4a9d105
Create Date: 2026-06-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8f3d21b9c47"
down_revision: Union[str, None] = "e7b2c4a9d105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_edit_tokens",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "revoked", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vendor_edit_tokens_vendor_id", "vendor_edit_tokens", ["vendor_id"]
    )
    op.create_index(
        "ix_vendor_edit_tokens_token_hash",
        "vendor_edit_tokens",
        ["token_hash"],
        unique=True,
    )

    op.create_table(
        "product_pending_changes",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("product_id", sa.String(length=30), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_pending_changes_vendor_id",
        "product_pending_changes",
        ["vendor_id"],
    )
    op.create_index(
        "ix_product_pending_changes_product_id",
        "product_pending_changes",
        ["product_id"],
    )
    op.create_index(
        "ix_product_pending_changes_status",
        "product_pending_changes",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("product_pending_changes")
    op.drop_table("vendor_edit_tokens")
