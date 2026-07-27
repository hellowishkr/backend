"""add occasion gift plans

Revision ID: c5a1e7d9b304
Revises: b2f7e9c4a106
Create Date: 2026-07-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5a1e7d9b304"
down_revision: Union[str, None] = "b2f7e9c4a106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "occasion_gift_plans",
        sa.Column("id", sa.String(length=30), nullable=False),
        sa.Column("user_id", sa.String(length=30), nullable=False),
        sa.Column("occasion_id", sa.String(length=30), nullable=False),
        sa.Column("contact_id", sa.String(length=30), nullable=True),
        sa.Column("budget_min", sa.Integer(), nullable=True),
        sa.Column("budget_max", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("interests", sa.JSON(), nullable=False),
        sa.Column("vibe_ids", sa.JSON(), nullable=False),
        sa.Column("desired_categories", sa.JSON(), nullable=False),
        sa.Column("excluded_categories", sa.JSON(), nullable=False),
        sa.Column("preferred_brands", sa.JSON(), nullable=False),
        sa.Column("disliked_brands", sa.JSON(), nullable=False),
        sa.Column("gift_format", sa.String(length=30), nullable=True),
        sa.Column("constraints", sa.JSON(), nullable=False),
        sa.Column("recipient_city", sa.String(length=80), nullable=True),
        sa.Column("selected_candidate_id", sa.String(length=80), nullable=True),
        sa.Column("selected_provider_id", sa.String(length=80), nullable=True),
        sa.Column("selected_brand", sa.String(length=120), nullable=True),
        sa.Column("current_step", sa.String(length=40), nullable=True),
        sa.Column("last_suggestions", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["occasion_id"], ["occasions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "occasion_id",
            name="uq_occasion_gift_plan_user_occasion",
        ),
    )
    op.create_index(
        op.f("ix_occasion_gift_plans_user_id"),
        "occasion_gift_plans",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_occasion_gift_plans_occasion_id"),
        "occasion_gift_plans",
        ["occasion_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_occasion_gift_plans_occasion_id"),
        table_name="occasion_gift_plans",
    )
    op.drop_index(
        op.f("ix_occasion_gift_plans_user_id"),
        table_name="occasion_gift_plans",
    )
    op.drop_table("occasion_gift_plans")
