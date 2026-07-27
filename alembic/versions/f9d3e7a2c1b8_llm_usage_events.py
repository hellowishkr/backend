"""Add llm_usage_events table for LLM cost tracking.

Revision ID: f9d3e7a2c1b8
Revises: 7f0d2c9a8b61
Create Date: 2026-05-10

Changes:
  - llm_usage_events: persistent tracking of every LLM call for cost accounting
    Captures: provider, model, purpose, phase, phone_hash, token usage,
    latency, cost estimate, success/failure, error reason, tool call count
  - Indexes on created_at and phone_hash for cost queries and user analysis
"""

from alembic import op
import sqlalchemy as sa

revision = "f9d3e7a2c1b8"
down_revision = "7f0d2c9a8b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("phone_hash", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_hit_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_miss_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), default=True, nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), default=False, nullable=False),
        sa.Column("tool_calls_count", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_llm_usage_created_at", "llm_usage_events", ["created_at"])
    op.create_index("ix_llm_usage_phone_hash", "llm_usage_events", ["phone_hash"])
    op.create_index("ix_llm_usage_purpose_created", "llm_usage_events", ["purpose", "created_at"])


def downgrade() -> None:
    op.drop_table("llm_usage_events")
