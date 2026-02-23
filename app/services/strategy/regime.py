"""Market regime detection — trending vs ranging vs volatile.

Used by AutoTrader to select the right strategy per symbol.
ADX measures trend strength, Bollinger Bandwidth measures volatility contraction.
"""

import logging
from enum import Enum

import pandas as pd

from app.services.strategy.indicators import adx, bollinger_bands

logger = logging.getLogger(__name__)


class RegimeType(str, Enum):
    """Market regime classification."""

    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


def detect_regime(
    df: pd.DataFrame,
    adx_trending: float = 25.0,
    adx_ranging: float = 20.0,
    bw_percentile_high: float = 60.0,
    bw_percentile_low: float = 40.0,
) -> RegimeType:
    """Classify the current market regime from OHLCV data.

    Uses the most recent ADX value and Bollinger Bandwidth percentile.

    Args:
        df: OHLCV DataFrame with columns: high, low, close (in cents).
            Needs at least 30 rows for reliable detection.
        adx_trending: ADX threshold above which market is trending.
        adx_ranging: ADX threshold below which market is ranging.
        bw_percentile_high: Bandwidth percentile above which = expanding.
        bw_percentile_low: Bandwidth percentile below which = contracting.

    Returns:
        RegimeType enum value.
    """
    if len(df) < 30:
        logger.warning("Not enough data for regime detection (%d rows), defaulting to VOLATILE", len(df))
        return RegimeType.VOLATILE

    adx_values = adx(df["high"], df["low"], df["close"])
    _, _, _, bandwidth = bollinger_bands(df["close"])

    current_adx = adx_values.iloc[-1]
    current_bw = bandwidth.iloc[-1]

    if pd.isna(current_adx) or pd.isna(current_bw):
        return RegimeType.VOLATILE

    # Bandwidth percentile relative to recent history
    bw_pct = (bandwidth.rank(pct=True) * 100).iloc[-1]

    if current_adx > adx_trending and bw_pct > bw_percentile_high:
        return RegimeType.TRENDING
    elif current_adx < adx_ranging and bw_pct < bw_percentile_low:
        return RegimeType.RANGING
    else:
        return RegimeType.VOLATILE
