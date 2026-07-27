"""add vendor_documents table for KYC/onboarding docs

Revision ID: a1d4f7b92c63
Revises: f1c6a93e2b07
Create Date: 2026-06-25 00:00:00.000000

If `alembic heads` shows more than one head, merge first, then set
down_revision to the merge revision.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1d4f7b92c63"
down_revision: Union[str, None] = "f1c6a93e2b07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendor_documents",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("vendor_id", sa.String(length=30), nullable=False),
        sa.Column("doc_type", sa.String(length=40), nullable=False),
        sa.Column("file_key", sa.String(length=500), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vendor_id", "doc_type", name="uq_vendor_doc_type"),
    )
    op.create_index(
        "ix_vendor_documents_vendor_id", "vendor_documents", ["vendor_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_documents_vendor_id", table_name="vendor_documents")
    op.drop_table("vendor_documents")
