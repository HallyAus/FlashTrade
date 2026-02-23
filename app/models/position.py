"""Open position model. Database is the source of truth, not the broker."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Position(Base):
    """Currently open positions. All money in cents."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)  # long, short
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stop_loss_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    unrealized_pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(datetime.UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(datetime.UTC)
    )
