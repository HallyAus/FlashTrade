"""OHLCV candlestick data model."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OHLCV(Base):
    """Price candlestick data. All prices stored in cents."""

    __tablename__ = "ohlcv"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)  # asx, us, crypto
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)  # 1m, 5m, 1h, 4h, 1d
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[int] = mapped_column(BigInteger, nullable=False)  # cents
    high: Mapped[int] = mapped_column(BigInteger, nullable=False)  # cents
    low: Mapped[int] = mapped_column(BigInteger, nullable=False)  # cents
    close: Mapped[int] = mapped_column(BigInteger, nullable=False)  # cents
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_ohlcv_symbol_tf_ts", "symbol", "timeframe", "timestamp", unique=True),
        Index("ix_ohlcv_market", "market"),
    )
