"""Mean reversion strategy: Bollinger Bands + RSI for ranging markets."""

import logging

import pandas as pd

from app.services.strategy.base import BaseStrategy, Signal
from app.services.strategy.indicators import atr, bollinger_bands, rsi

logger = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """Bollinger Band mean reversion strategy.

    Entry (BUY): Price closes below lower band AND RSI < 35.
    Exit (SELL): Price crosses above middle band.
    Stop loss: 1.5x ATR below entry.

    Best suited for RANGING regime (ADX < 20).
    """

    @property
    def name(self) -> str:
        return "meanrev"

    def generate_signals(
        self, df: pd.DataFrame, symbol: str, market: str
    ) -> list[Signal]:
        if len(df) < 25:
            return []

        close = df["close"]
        upper, middle, lower, _ = bollinger_bands(close)
        rsi_values = rsi(close)
        atr_values = atr(df["high"], df["low"], close)

        current_close = int(close.iloc[-1])
        current_lower = lower.iloc[-1]
        current_middle = middle.iloc[-1]
        current_upper = upper.iloc[-1]
        current_rsi = rsi_values.iloc[-1]
        prev_close = close.iloc[-2]
        current_atr = atr_values.iloc[-1]

        if any(pd.isna(v) for v in [current_rsi, current_lower, current_atr, prev_close]):
            return []

        signals = []

        # BUY signal: price below lower band + RSI oversold
        if current_close < current_lower and current_rsi < 35:
            stop_loss = int(current_close - 1.5 * current_atr)
            # Strength based on how far below the band
            distance_below = (current_lower - current_close) / current_lower if current_lower > 0 else 0
            strength = min(1.0, distance_below * 10 + 0.3)
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="buy",
                    strength=strength,
                    stop_loss_cents=max(1, stop_loss),
                    price_cents=current_close,
                    reason=(
                        f"Price ({current_close}) below lower Bollinger Band ({current_lower:.0f}), "
                        f"RSI oversold ({current_rsi:.1f})"
                    ),
                    strategy_name=self.name,
                    indicator_data={
                        "rsi": round(current_rsi, 2),
                        "bb_lower": round(current_lower, 2),
                        "bb_middle": round(float(current_middle), 2),
                        "bb_upper": round(float(current_upper), 2),
                        "atr": round(current_atr, 2),
                    },
                )
            )

        # SELL signal: price crosses above middle band (take profit)
        # Stop-loss = current price (closing a long, not opening a short)
        if prev_close < current_middle and current_close >= current_middle:
            stop_loss = current_close
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="sell",
                    strength=0.6,
                    stop_loss_cents=stop_loss,
                    price_cents=current_close,
                    reason=f"Price crossed above middle Bollinger Band ({current_middle:.0f}), mean reversion target hit",
                    strategy_name=self.name,
                    indicator_data={
                        "rsi": round(current_rsi, 2),
                        "bb_middle": round(float(current_middle), 2),
                        "atr": round(current_atr, 2),
                    },
                )
            )

        # SELL signal: price above upper band (overextended)
        # Stop-loss = current price (closing a long, not opening a short)
        if current_close > current_upper and current_rsi > 65:
            stop_loss = current_close
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="sell",
                    strength=0.8,
                    stop_loss_cents=stop_loss,
                    price_cents=current_close,
                    reason=(
                        f"Price ({current_close}) above upper Bollinger Band ({current_upper:.0f}), "
                        f"RSI elevated ({current_rsi:.1f})"
                    ),
                    strategy_name=self.name,
                    indicator_data={
                        "rsi": round(current_rsi, 2),
                        "bb_upper": round(float(current_upper), 2),
                        "atr": round(current_atr, 2),
                    },
                )
            )

        return signals
