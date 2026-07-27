import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Text, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConversationFlow(str, enum.Enum):
    IDLE = "IDLE"
    REGISTERING = "REGISTERING"
    ADDING_OCCASION = "ADDING_OCCASION"
    BROWSING_GIFTS = "BROWSING_GIFTS"
    PURCHASING = "PURCHASING"
    VIEWING_OCCASIONS = "VIEWING_OCCASIONS"
    VIEWING_ORDERS = "VIEWING_ORDERS"
    SETTING_UP_CELEBRANT = "SETTING_UP_CELEBRANT"
    FRIEND_PURCHASING = "FRIEND_PURCHASING"


class ConversationState(Base):
    __tablename__ = "conversation_states"

    phone: Mapped[str] = mapped_column(String(15), primary_key=True)
    flow: Mapped[ConversationFlow] = mapped_column(Enum(ConversationFlow), default=ConversationFlow.IDLE)
    step: Mapped[str | None] = mapped_column(String(50))  # current step within the flow
    temp_data: Mapped[str | None] = mapped_column(Text)  # JSON blob for in-progress data
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
