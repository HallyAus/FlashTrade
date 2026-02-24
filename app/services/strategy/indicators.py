"""Technical indicators for trading strategies.

Pure pandas/numpy implementations — no external TA library needed.
All price inputs expected in cents (integer), outputs in cents where applicable.
"""

import numpy as np
import pandas as pd


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0-100).

    Args:
        series: Price series (typically close prices in cents).
        period: Lookback period.

    Returns:
        RSI values as a Series.
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Moving Average Convergence Divergence.

    Returns:
        Tuple of (macd_line, signal_line, histogram).
    """
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.

    Returns:
        Tuple of (upper, middle, lower, bandwidth).
        Bandwidth = (upper - lower) / middle, useful for regime detection.
    """
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = (upper - lower) / middle.replace(0, np.nan)
    return upper, middle, lower, bandwidth


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range — volatility measure for stop-loss sizing.

    Args:
        high: High prices in cents.
        low: Low prices in cents.
        close: Close prices in cents.
        period: Lookback period.

    Returns:
        ATR values in cents.
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period).mean()


def ema(series: pd.Series, period: int = 50) -> pd.Series:
    """Exponential Moving Average.

    Args:
        series: Price series (typically close prices in cents).
        period: Lookback period.

    Returns:
        EMA values as a Series.
    """
    return series.ewm(span=period, adjust=False).mean()


def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """Simple Moving Average of volume for relative volume comparison.

    Args:
        volume: Volume series.
        period: Lookback period.

    Returns:
        Volume SMA values as a Series.
    """
    return volume.rolling(window=period).mean()


def donchian_channel(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian Channel — breakout bands used in Turtle Trading.

    Args:
        high: High prices in cents.
        low: Low prices in cents.
        period: Lookback period (number of bars).

    Returns:
        Tuple of (upper, lower, middle) where:
        - upper = highest high over `period` bars
        - lower = lowest low over `period` bars
        - middle = (upper + lower) / 2
    """
    upper = high.rolling(window=period).max()
    lower = low.rolling(window=period).min()
    middle = (upper + lower) / 2
    return upper, lower, middle


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index — trend strength (0-100).

    ADX > 25 = strong trend, ADX < 20 = weak/no trend.
    """
    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_values = atr(high, low, close, period)

    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr_values.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr_values.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(alpha=1 / period, min_periods=period).mean()
