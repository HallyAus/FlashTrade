"""Trade record model."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Trade(Base):
    """Record of every trade executed. All money in cents."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # buy, sell
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)  # market, limit, stop
    quantity_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)  # position size in cents
    price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)  # execution price in cents
    stop_loss_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending, filled, cancelled, rejected
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why this trade was taken
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(datetime.UTC)
    )
    realized_pnl_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
