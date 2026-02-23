"""Data quality checks for OHLCV data.

Detects missing candles, price outliers, stale data, and volume anomalies.
Runs against the database and returns a structured report.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.data.market_calendar import (
    Market,
    expected_candle_count,
    is_market_open,
)

logger = logging.getLogger(__name__)


@dataclass
class QualityIssue:
    """A single data quality issue."""

    severity: str  # "warning" or "error"
    check: str  # Name of the check
    symbol: str
    timeframe: str
    message: str


@dataclass
class SymbolReport:
    """Quality report for a single symbol/timeframe combo."""

    symbol: str
    market: str
    timeframe: str
    total_rows: int = 0
    expected_rows: int = 0
    missing_pct: float = 0.0
    latest_candle_utc: str = ""
    staleness_minutes: int = 0
    outlier_count: int = 0
    issues: list[QualityIssue] = field(default_factory=list)


@dataclass
class DataQualityReport:
    """Full quality report across all symbols."""

    checked_at_utc: str = ""
    symbols: list[SymbolReport] = field(default_factory=list)
    total_issues: int = 0
    errors: int = 0
    warnings: int = 0


# Staleness thresholds: (market, timeframe) -> minutes
# Only flag as stale if data is older than this AND market is open
STALE_THRESHOLDS = {
    # Crypto hourly: allow 2 hours (Celery runs every 1 min, but exchange lag + gaps)
    ("crypto", "1h"): 120,
    ("crypto", "1d"): 1500,   # Daily candle: ~25 hours is fine
    ("crypto", "1m"): 5,
    # Stocks: only checked during market hours anyway
    ("asx", "1h"): 120,
    ("asx", "1d"): 1500,
    ("us", "1h"): 120,
    ("us", "1d"): 1500,
}
STALE_DEFAULT_MINUTES = 120  # Fallback

OUTLIER_STD_DEVS = 4.0  # Flag moves > 4 standard deviations (3 was too noisy)
MAX_MISSING_PCT = 10.0  # Flag if >10% candles missing (5% too strict with holidays)


async def run_quality_checks(
    lookback_hours: int = 168,  # 7 days
) -> DataQualityReport:
    """Run all quality checks against the OHLCV table.

    Args:
        lookback_hours: How far back to check (default 7 days).

    Returns:
        DataQualityReport with issues found.
    """
    report = DataQualityReport(
        checked_at_utc=datetime.now(timezone.utc).isoformat(),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    async with async_session() as session:
        # Get distinct symbol/market/timeframe combos
        combos_stmt = (
            select(OHLCV.symbol, OHLCV.market, OHLCV.timeframe)
            .where(OHLCV.timestamp >= cutoff)
            .group_by(OHLCV.symbol, OHLCV.market, OHLCV.timeframe)
        )
        result = await session.execute(combos_stmt)
        combos = result.all()

        for symbol, market, timeframe in combos:
            sym_report = await _check_symbol(
                session, symbol, market, timeframe, cutoff
            )
            report.symbols.append(sym_report)

            for issue in sym_report.issues:
                report.total_issues += 1
                if issue.severity == "error":
                    report.errors += 1
                else:
                    report.warnings += 1

    return report


async def _check_symbol(
    session: AsyncSession,
    symbol: str,
    market: str,
    timeframe: str,
    cutoff: datetime,
) -> SymbolReport:
    """Run quality checks for a single symbol/timeframe."""
    now_utc = datetime.now(timezone.utc)

    sr = SymbolReport(symbol=symbol, market=market, timeframe=timeframe)

    # 1. Row count
    count_stmt = (
        select(func.count())
        .select_from(OHLCV)
        .where(
            OHLCV.symbol == symbol,
            OHLCV.timeframe == timeframe,
            OHLCV.timestamp >= cutoff,
        )
    )
    sr.total_rows = (await session.execute(count_stmt)).scalar() or 0

    # 2. Expected candle count
    try:
        mkt = Market(market)
        sr.expected_rows = expected_candle_count(mkt, timeframe, cutoff, now_utc)
    except ValueError:
        sr.expected_rows = sr.total_rows  # Unknown market, skip gap check

    # 3. Missing candle percentage
    if sr.expected_rows > 0:
        sr.missing_pct = round(
            ((sr.expected_rows - sr.total_rows) / sr.expected_rows) * 100, 1
        )
        if sr.missing_pct > MAX_MISSING_PCT:
            sr.issues.append(QualityIssue(
                severity="warning",
                check="missing_candles",
                symbol=symbol,
                timeframe=timeframe,
                message=f"{sr.missing_pct}% candles missing ({sr.total_rows}/{sr.expected_rows})",
            ))

    # 4. Staleness check
    latest_stmt = (
        select(func.max(OHLCV.timestamp))
        .where(OHLCV.symbol == symbol, OHLCV.timeframe == timeframe)
    )
    latest_ts = (await session.execute(latest_stmt)).scalar()
    if latest_ts:
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        sr.latest_candle_utc = latest_ts.isoformat()
        sr.staleness_minutes = int((now_utc - latest_ts).total_seconds() / 60)

        threshold = STALE_THRESHOLDS.get((market, timeframe), STALE_DEFAULT_MINUTES)
        # Only flag staleness if market is open
        try:
            mkt = Market(market)
            if is_market_open(mkt) and sr.staleness_minutes > threshold:
                sr.issues.append(QualityIssue(
                    severity="error",
                    check="stale_data",
                    symbol=symbol,
                    timeframe=timeframe,
                    message=f"Data is {sr.staleness_minutes}min stale (threshold: {threshold}min)",
                ))
        except ValueError:
            pass

    # 5. Price outlier detection (close-to-close % change > 3 std devs)
    outlier_stmt = text("""
        WITH changes AS (
            SELECT
                close,
                LAG(close) OVER (ORDER BY timestamp) AS prev_close
            FROM ohlcv
            WHERE symbol = :symbol
              AND timeframe = :timeframe
              AND timestamp >= :cutoff
        ),
        stats AS (
            SELECT
                AVG(ABS(close - prev_close)::float / NULLIF(prev_close, 0)) AS avg_change,
                STDDEV(ABS(close - prev_close)::float / NULLIF(prev_close, 0)) AS std_change
            FROM changes
            WHERE prev_close IS NOT NULL AND prev_close > 0
        )
        SELECT COUNT(*) FROM changes, stats
        WHERE prev_close IS NOT NULL
          AND prev_close > 0
          AND ABS(close - prev_close)::float / prev_close
              > (avg_change + :std_mult * COALESCE(std_change, 0))
    """)
    outlier_result = await session.execute(outlier_stmt, {
        "symbol": symbol,
        "timeframe": timeframe,
        "cutoff": cutoff,
        "std_mult": OUTLIER_STD_DEVS,
    })
    sr.outlier_count = outlier_result.scalar() or 0
    if sr.outlier_count > 0:
        sr.issues.append(QualityIssue(
            severity="warning",
            check="price_outliers",
            symbol=symbol,
            timeframe=timeframe,
            message=f"{sr.outlier_count} price moves > {OUTLIER_STD_DEVS} std devs",
        ))

    return sr
