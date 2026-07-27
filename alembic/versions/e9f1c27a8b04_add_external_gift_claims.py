"""add external_gift_claims table

Revision ID: e9f1c27a8b04
Revises: b8e3c04f1a22
Create Date: 2026-04-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f1c27a8b04"
down_revision: Union[str, None] = "b8e3c04f1a22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_gift_claims",
        sa.Column("id", sa.String(length=30), primary_key=True),
        sa.Column("occasion_id", sa.String(length=30), nullable=False),
        sa.Column("buyer_phone", sa.String(length=15), nullable=False),
        sa.Column("item_description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("brand_name", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["occasion_id"], ["occasions.id"]),
    )
    op.create_index(
        "ix_external_gift_claims_occasion",
        "external_gift_claims",
        ["occasion_id"],
    )
    op.create_index(
        op.f("ix_external_gift_claims_buyer_phone"),
        "external_gift_claims",
        ["buyer_phone"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_external_gift_claims_buyer_phone"),
        table_name="external_gift_claims",
    )
    op.drop_index(
        "ix_external_gift_claims_occasion",
        table_name="external_gift_claims",
    )
    op.drop_table("external_gift_claims")
