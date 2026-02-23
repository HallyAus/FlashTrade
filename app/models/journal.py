"""Trade journal for logging every decision with reasoning."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JournalEntry(Base):
    """Every trade decision is logged here — win or lose, and why."""

    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # entry, exit, skip, stop_hit
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON of indicator values
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # P&L if exit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(datetime.UTC)
    )
