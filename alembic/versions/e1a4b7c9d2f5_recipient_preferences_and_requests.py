"""Add recipient_preferences and preference_requests tables.

Revision ID: e1a4b7c9d2f5
Revises: d9f3c1a8b4e7
Create Date: 2026-05-08

Changes:
  - recipient_preferences: stores structured gift preferences keyed by
    recipient phone number. Populated from WhatsApp reply parsing or
    manual entry by the gift-giver.
  - preference_requests: tracks requests sent to recipients asking for
    their gift preferences. State machine: pending → responded/expired/cancelled.
    Deduplicates to one pending request per (recipient_phone, occasion_id).
"""

from alembic import op
import sqlalchemy as sa

revision = "e1a4b7c9d2f5"
down_revision = "d9f3c1a8b4e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipient_preferences",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("recipient_phone", sa.String(15), nullable=False, unique=True),
        sa.Column("preferred_categories", sa.Text(), nullable=True),
        sa.Column("disliked_categories", sa.Text(), nullable=True),
        sa.Column("preferred_brands", sa.Text(), nullable=True),
        sa.Column("disliked_brands", sa.Text(), nullable=True),
        sa.Column("preferred_budget_min", sa.Integer(), nullable=True),
        sa.Column("preferred_budget_max", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("wishlist_links", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="direct_response"),
        sa.Column("collected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_recip_pref_phone", "recipient_preferences", ["recipient_phone"])

    op.create_table(
        "preference_requests",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("recipient_phone", sa.String(15), nullable=False),
        sa.Column("occasion_id", sa.String(30), nullable=True),
        sa.Column("requester_phone", sa.String(15), nullable=False),
        sa.Column("notify_phones", sa.Text(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("anonymous", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pref_req_recipient", "preference_requests", ["recipient_phone"])
    op.create_index(
        "ix_pref_req_requester", "preference_requests", ["requester_phone"]
    )
    op.create_index(
        "ix_pref_req_pending",
        "preference_requests",
        ["recipient_phone", "occasion_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("preference_requests")
    op.drop_table("recipient_preferences")
