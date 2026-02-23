"""OHLCV data ingestion — fetches candles and persists to PostgreSQL.

Handles crypto (via CCXT) and stocks (via yfinance).
Uses upsert logic so re-runs don't create duplicates.
"""

import logging
from datetime import datetime, timezone
from typing import Literal

import ccxt
import pandas as pd
import yfinance as yf
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

# Symbols per market
CRYPTO_SYMBOLS = {
    "BTC/AUD": "BTC",
    "ETH/AUD": "ETH",
    "SOL/AUD": "SOL",
}
CRYPTO_FALLBACK = {
    "BTC/USDT": "BTC",
    "ETH/USDT": "ETH",
    "SOL/USDT": "SOL",
}
ASX_SYMBOLS = ["BHP.AX", "CBA.AX", "CSL.AX"]
US_SYMBOLS = ["SPY", "AAPL", "NVDA"]

# CCXT timeframe mapping
TIMEFRAME_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


async def upsert_ohlcv_batch(session: AsyncSession, rows: list[dict]) -> int:
    """Bulk upsert OHLCV rows. Returns count of rows affected.

    Uses PostgreSQL ON CONFLICT (symbol, timeframe, timestamp) DO UPDATE
    to handle re-runs without duplicates.
    """
    if not rows:
        return 0

    stmt = text("""
        INSERT INTO ohlcv (symbol, market, timeframe, timestamp, open, high, low, close, volume)
        VALUES (:symbol, :market, :timeframe, :timestamp, :open, :high, :low, :close, :volume)
        ON CONFLICT (symbol, timeframe, timestamp)
        DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume
    """)

    await session.execute(stmt, rows)
    await session.commit()
    return len(rows)


async def ingest_crypto_ohlcv(
    timeframe: str = "1h",
    limit: int = 100,
) -> int:
    """Fetch recent crypto OHLCV candles and write to DB.

    Returns total rows upserted.
    """
    exchange = None
    symbols = CRYPTO_SYMBOLS
    currency_note = "AUD"

    try:
        exchange = ccxt.swyftx({"enableRateLimit": True, "timeout": 15000})
        exchange.load_markets()
        logger.info("Ingestion: using Swyftx (%s pairs)", currency_note)
    except Exception as e:
        logger.warning("Swyftx unavailable (%s), trying Binance", e)
        try:
            exchange = ccxt.binance({"enableRateLimit": True, "timeout": 15000})
            exchange.load_markets()
            symbols = CRYPTO_FALLBACK
            currency_note = "USDT"
            logger.info("Ingestion: using Binance (USDT pairs)")
        except Exception as e2:
            logger.error("Both exchanges failed: %s", e2)
            return 0

    total = 0
    async with async_session() as session:
        for pair, short_name in symbols.items():
            try:
                candles = exchange.fetch_ohlcv(pair, timeframe, limit=limit)
                rows = []
                for c in candles:
                    ts_ms, o, h, l, cl, vol = c
                    rows.append({
                        "symbol": short_name,
                        "market": "crypto",
                        "timeframe": timeframe,
                        "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                        "open": int(round(o * 100)),
                        "high": int(round(h * 100)),
                        "low": int(round(l * 100)),
                        "close": int(round(cl * 100)),
                        "volume": int(round(vol)),
                    })
                count = await upsert_ohlcv_batch(session, rows)
                total += count
                logger.info("Ingested %d candles for %s (%s)", count, short_name, timeframe)
            except Exception as e:
                logger.error("Failed to ingest %s: %s", pair, e)

    return total


async def ingest_stock_ohlcv(
    market: Literal["asx", "us"],
    timeframe: str = "1d",
    period: str = "5d",
) -> int:
    """Fetch stock OHLCV from yfinance and write to DB.

    Args:
        market: "asx" or "us"
        timeframe: yfinance interval (1m, 5m, 15m, 1h, 1d)
        period: yfinance period (1d, 5d, 1mo, 3mo, 6mo, 1y)

    Returns total rows upserted.
    """
    symbols = ASX_SYMBOLS if market == "asx" else US_SYMBOLS
    currency = "AUD" if market == "asx" else "USD"

    total = 0
    async with async_session() as session:
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                df = ticker.history(period=period, interval=timeframe)

                if df.empty:
                    logger.warning("No data returned for %s", sym)
                    continue

                rows = []
                for ts, row in df.iterrows():
                    # yfinance returns timezone-aware DatetimeIndex
                    if hasattr(ts, "to_pydatetime"):
                        ts_dt = ts.to_pydatetime()
                    else:
                        ts_dt = ts

                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)

                    rows.append({
                        "symbol": sym,
                        "market": market,
                        "timeframe": timeframe,
                        "timestamp": ts_dt,
                        "open": int(round(row["Open"] * 100)),
                        "high": int(round(row["High"] * 100)),
                        "low": int(round(row["Low"] * 100)),
                        "close": int(round(row["Close"] * 100)),
                        "volume": int(row.get("Volume", 0)),
                    })

                count = await upsert_ohlcv_batch(session, rows)
                total += count
                logger.info("Ingested %d candles for %s (%s/%s)", count, sym, timeframe, period)
            except Exception as e:
                logger.error("Failed to ingest %s: %s", sym, e)

    return total


async def backfill_all(period: str = "6mo") -> dict[str, int]:
    """Backfill all markets with historical data.

    Args:
        period: How far back to go (e.g., "6mo", "1y").

    Returns dict of market -> rows ingested.
    """
    results: dict[str, int] = {}

    # Crypto: CCXT supports fetching large batches via since parameter
    # For backfill, fetch 1h candles (max ~500 per request for most exchanges)
    logger.info("Backfilling crypto (1h candles, ~%s)...", period)
    results["crypto_1h"] = await _backfill_crypto("1h", period)

    logger.info("Backfilling crypto (1d candles, ~%s)...", period)
    results["crypto_1d"] = await _backfill_crypto("1d", period)

    # ASX stocks: daily candles
    logger.info("Backfilling ASX stocks (1d, %s)...", period)
    results["asx_1d"] = await ingest_stock_ohlcv("asx", "1d", period)

    # ASX stocks: hourly candles (yfinance limits to ~730 days for 1h)
    logger.info("Backfilling ASX stocks (1h, %s)...", period)
    results["asx_1h"] = await ingest_stock_ohlcv("asx", "1h", period)

    # US stocks: daily candles
    logger.info("Backfilling US stocks (1d, %s)...", period)
    results["us_1d"] = await ingest_stock_ohlcv("us", "1d", period)

    # US stocks: hourly candles
    logger.info("Backfilling US stocks (1h, %s)...", period)
    results["us_1h"] = await ingest_stock_ohlcv("us", "1h", period)

    total = sum(results.values())
    logger.info("Backfill complete. Total rows: %d | Breakdown: %s", total, results)
    return results


async def _backfill_crypto(timeframe: str, period: str) -> int:
    """Backfill crypto OHLCV by paginating through historical data."""
    # Map period string to milliseconds
    period_ms = _period_to_ms(period)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = now_ms - period_ms

    exchange = None
    symbols = CRYPTO_SYMBOLS

    try:
        exchange = ccxt.swyftx({"enableRateLimit": True, "timeout": 15000})
        exchange.load_markets()
    except Exception:
        try:
            exchange = ccxt.binance({"enableRateLimit": True, "timeout": 15000})
            exchange.load_markets()
            symbols = CRYPTO_FALLBACK
        except Exception as e:
            logger.error("No exchange available for crypto backfill: %s", e)
            return 0

    total = 0
    async with async_session() as session:
        for pair, short_name in symbols.items():
            cursor = since_ms
            pair_total = 0
            try:
                while cursor < now_ms:
                    candles = exchange.fetch_ohlcv(
                        pair, timeframe, since=cursor, limit=500
                    )
                    if not candles:
                        break

                    rows = []
                    for c in candles:
                        ts_ms, o, h, l, cl, vol = c
                        rows.append({
                            "symbol": short_name,
                            "market": "crypto",
                            "timeframe": timeframe,
                            "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                            "open": int(round(o * 100)),
                            "high": int(round(h * 100)),
                            "low": int(round(l * 100)),
                            "close": int(round(cl * 100)),
                            "volume": int(round(vol)),
                        })

                    count = await upsert_ohlcv_batch(session, rows)
                    pair_total += count

                    # Move cursor past the last candle
                    last_ts = candles[-1][0]
                    if last_ts <= cursor:
                        break
                    cursor = last_ts + 1

                logger.info("Backfilled %d candles for %s (%s)", pair_total, short_name, timeframe)
                total += pair_total
            except Exception as e:
                logger.error("Backfill failed for %s: %s", pair, e)

    return total


def _period_to_ms(period: str) -> int:
    """Convert period string like '6mo' or '1y' to milliseconds."""
    unit = period[-1] if not period.endswith("mo") else "mo"
    if period.endswith("mo"):
        num = int(period[:-2])
        return num * 30 * 24 * 60 * 60 * 1000
    elif period.endswith("y"):
        num = int(period[:-1])
        return num * 365 * 24 * 60 * 60 * 1000
    elif period.endswith("d"):
        num = int(period[:-1])
        return num * 24 * 60 * 60 * 1000
    else:
        # Default 6 months
        return 180 * 24 * 60 * 60 * 1000
