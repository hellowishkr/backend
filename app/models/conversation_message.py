"""Conversation message history — stores every message between user and AI agent."""

import enum
from datetime import datetime

from sqlalchemy import Integer, String, Text, DateTime, Enum, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageRole(str, enum.Enum):
    USER = "user"
    MODEL = "model"
    TOOL_CALL = "tool_call"
    TOOL_RESPONSE = "tool_response"


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(15), nullable=False)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(String(50))
    tool_args: Mapped[str | None] = mapped_column(Text)
    tool_result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_conv_msg_phone_created", "phone", "created_at"),
    )
