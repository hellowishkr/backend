"""marketplace provider policy fields

Revision ID: f2b7c9d4e6a1
Revises: 95b02584a118
Create Date: 2026-05-13 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b7c9d4e6a1"
down_revision: Union[str, None] = "95b02584a118"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendors", sa.Column("fulfillment_mode", sa.String(length=20), nullable=True))
    op.add_column("vendors", sa.Column("delivery_policy", sa.Text(), nullable=True))
    op.add_column("vendors", sa.Column("return_policy", sa.Text(), nullable=True))
    op.add_column("vendors", sa.Column("cancellation_policy", sa.Text(), nullable=True))
    op.add_column("vendors", sa.Column("delivery_lead_time_hours", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("same_day_cutoff_hour", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("min_price", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("max_price", sa.Integer(), nullable=True))
    op.add_column("vendors", sa.Column("reliability_score", sa.Float(), nullable=True))
    op.execute("UPDATE vendors SET fulfillment_mode = 'manual' WHERE fulfillment_mode IS NULL")
    op.execute("UPDATE vendors SET reliability_score = 0.5 WHERE reliability_score IS NULL")
    op.alter_column("vendors", "fulfillment_mode", nullable=False)
    op.alter_column("vendors", "reliability_score", nullable=False)

    op.add_column("gift_catalog", sa.Column("fulfillment_mode", sa.String(length=20), nullable=True))
    op.add_column("gift_catalog", sa.Column("fulfillment_type", sa.String(length=40), nullable=True))
    op.add_column("gift_catalog", sa.Column("delivery_policy", sa.Text(), nullable=True))
    op.add_column("gift_catalog", sa.Column("return_policy", sa.Text(), nullable=True))
    op.add_column("gift_catalog", sa.Column("cancellation_policy", sa.Text(), nullable=True))
    op.add_column("gift_catalog", sa.Column("delivery_lead_time_hours", sa.Integer(), nullable=True))
    op.add_column("gift_catalog", sa.Column("provider_health", sa.String(length=20), nullable=True))
    op.add_column("gift_catalog", sa.Column("reliability_score", sa.Float(), nullable=True))


def downgrade() -> None:
    for column in (
        "reliability_score",
        "provider_health",
        "delivery_lead_time_hours",
        "cancellation_policy",
        "return_policy",
        "delivery_policy",
        "fulfillment_type",
        "fulfillment_mode",
    ):
        op.drop_column("gift_catalog", column)
    for column in (
        "reliability_score",
        "max_price",
        "min_price",
        "same_day_cutoff_hour",
        "delivery_lead_time_hours",
        "cancellation_policy",
        "return_policy",
        "delivery_policy",
        "fulfillment_mode",
    ):
        op.drop_column("vendors", column)
