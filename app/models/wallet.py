import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, DateTime, Numeric, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WalletTxnType(str, enum.Enum):
    TOP_UP = "TOP_UP"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    type: Mapped[WalletTxnType] = mapped_column(Enum(WalletTxnType))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    reference: Mapped[str | None] = mapped_column(String(50))  # order ID or top-up reference
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
