"""Momentum strategy: RSI + MACD crossover for trending markets."""

import logging

import pandas as pd

from app.services.strategy.base import BaseStrategy, Signal
from app.services.strategy.indicators import atr, macd, rsi

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """RSI + MACD crossover momentum strategy.

    Entry (BUY): RSI crosses above rsi_entry from below AND MACD histogram turns positive.
    Exit (SELL): RSI crosses above rsi_exit OR MACD histogram turns negative.
    Stop loss: atr_stop_multiplier × ATR below entry price.

    Best suited for TRENDING regime (ADX > 25).
    """

    def __init__(
        self,
        rsi_entry: float = 30.0,
        rsi_exit: float = 70.0,
        atr_stop_multiplier: float = 2.0,
    ) -> None:
        self._rsi_entry = rsi_entry
        self._rsi_exit = rsi_exit
        self._atr_stop_multiplier = atr_stop_multiplier

    @property
    def name(self) -> str:
        return "momentum"

    def generate_signals(
        self, df: pd.DataFrame, symbol: str, market: str
    ) -> list[Signal]:
        if len(df) < 30:
            return []

        close = df["close"]
        rsi_values = rsi(close)
        _, _, macd_hist = macd(close)
        atr_values = atr(df["high"], df["low"], close)

        current_rsi = rsi_values.iloc[-1]
        prev_rsi = rsi_values.iloc[-2]
        current_hist = macd_hist.iloc[-1]
        prev_hist = macd_hist.iloc[-2]
        current_atr = atr_values.iloc[-1]
        current_price = int(close.iloc[-1])

        if any(pd.isna(v) for v in [current_rsi, prev_rsi, current_hist, prev_hist, current_atr]):
            return []

        signals = []

        # BUY signal: RSI crosses above entry threshold + MACD histogram turns positive
        if (prev_rsi < self._rsi_entry and current_rsi >= self._rsi_entry
                and current_hist > 0 and prev_hist <= 0):
            stop_loss = int(current_price - self._atr_stop_multiplier * current_atr)
            strength = min(1.0, (current_hist / current_atr) if current_atr > 0 else 0.5)
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="buy",
                    strength=strength,
                    stop_loss_cents=max(1, stop_loss),
                    price_cents=current_price,
                    reason=f"RSI crossed above {self._rsi_entry:.0f} ({current_rsi:.1f}), MACD histogram turned positive ({current_hist:.0f})",
                    strategy_name=self.name,
                    indicator_data={
                        "rsi": round(current_rsi, 2),
                        "macd_hist": round(current_hist, 2),
                        "atr": round(current_atr, 2),
                    },
                )
            )

        # SELL signal: RSI > exit threshold or MACD histogram turns negative
        if current_rsi > self._rsi_exit or (current_hist < 0 and prev_hist >= 0):
            reason_parts = []
            if current_rsi > self._rsi_exit:
                reason_parts.append(f"RSI overbought ({current_rsi:.1f})")
            if current_hist < 0 and prev_hist >= 0:
                reason_parts.append(f"MACD histogram turned negative ({current_hist:.0f})")
            stop_loss = current_price
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="sell",
                    strength=min(1.0, current_rsi / 100),
                    stop_loss_cents=stop_loss,
                    price_cents=current_price,
                    reason=", ".join(reason_parts),
                    strategy_name=self.name,
                    indicator_data={
                        "rsi": round(current_rsi, 2),
                        "macd_hist": round(current_hist, 2),
                        "atr": round(current_atr, 2),
                    },
                )
            )

        return signals
