"""LLM usage tracking for cost accounting and monitoring."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Numeric, String, Text, Boolean, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LlmUsageEvent(Base):
    """Track every LLM call: input/output tokens, cost, latency, success/failure."""

    __tablename__ = "llm_usage_events"

    # Identifiers
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # LLM call metadata
    provider: Mapped[str] = mapped_column(String(32))  # 'gemini', 'deepseek', 'openai', etc.
    model: Mapped[str] = mapped_column(String(64))     # 'gemini-2.5-flash', etc.
    purpose: Mapped[str] = mapped_column(String(32))   # 'main_agent', 'classifier', 'embedding'
    phase: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 'browse', 'friend', etc. (null for classifier)

    # Request context
    conversation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # ADK session_id
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)       # SHA256[:16] of phone

    # Token usage
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_hit_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # Gemini implicit cache
    cache_miss_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Cost
    estimated_cost_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 8), nullable=True
    )

    # Performance
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Outcome
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # 'timeout', 'api_error', 'tool_loop_limit_exceeded'
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)

    # Turn context
    tool_calls_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # # of tool calls in this turn
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)        # extra context as needed

    def __repr__(self) -> str:
        return (
            f"<LlmUsageEvent {self.id} | {self.provider}:{self.model} | "
            f"purpose={self.purpose} | cost=${self.estimated_cost_usd}>"
        )
