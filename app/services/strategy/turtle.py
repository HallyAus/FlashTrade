"""Turtle Trading strategies — Donchian channel breakout with pyramiding.

Two variants:
- TurtleCryptoStrategy: Shorter channels, wider stops, fee-aware for 24/7 crypto.
- TurtleStocksStrategy: Classic Turtle parameters tuned for daily stock bars.

Both implement the original Turtle system concepts:
- System 1 (short-term) and System 2 (long-term) breakout entries
- N-based (ATR) position sizing and stop placement
- Pyramiding up to max_pyramids levels at 0.5N intervals
- Channel-based exits (lowest low over exit_period)
"""

import logging

import pandas as pd

from app.services.strategy.base import BaseStrategy, Signal
from app.services.strategy.indicators import atr, donchian_channel

logger = logging.getLogger(__name__)


class _TurtleBase(BaseStrategy):
    """Core Donchian breakout logic shared by crypto and stocks variants.

    Subclasses set default channel periods, stop multipliers, and pyramid limits.
    """

    def __init__(
        self,
        entry_period: int = 20,
        exit_period: int = 10,
        long_entry_period: int = 55,
        atr_period: int = 20,
        stop_multiplier: float = 2.0,
        pyramid_atr_step: float = 0.5,
        max_pyramids: int = 4,
    ) -> None:
        self._entry_period = entry_period
        self._exit_period = exit_period
        self._long_entry_period = long_entry_period
        self._atr_period = atr_period
        self._stop_multiplier = stop_multiplier
        self._pyramid_atr_step = pyramid_atr_step
        self._max_pyramids = max_pyramids

        # Internal pyramid tracking (reset per symbol evaluation cycle)
        self._pyramid_count: int = 0
        self._last_entry_price: int = 0
        self._current_n: float = 0.0  # Current ATR (N)

    @property
    def name(self) -> str:
        raise NotImplementedError

    def generate_signals(
        self, df: pd.DataFrame, symbol: str, market: str
    ) -> list[Signal]:
        """Generate Turtle Trading signals from OHLCV data.

        Logic:
        1. Compute Donchian channels (System 1 short, System 2 long)
        2. Compute ATR (N) for sizing and stops
        3. BUY if close breaks above upper channel (new entry or pyramid)
        4. SELL if close breaks below exit channel lower band
        """
        min_bars = max(self._entry_period, self._long_entry_period, self._atr_period) + 5
        if len(df) < min_bars:
            return []

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Donchian channels — use shifted values to avoid look-ahead
        # Entry channel: highest high / lowest low over entry_period (prior bars)
        entry_upper, entry_lower, _ = donchian_channel(high.shift(1), low.shift(1), self._entry_period)
        # Long-term channel for System 2 entries
        long_upper, _, _ = donchian_channel(high.shift(1), low.shift(1), self._long_entry_period)
        # Exit channel (shorter period)
        _, exit_lower, _ = donchian_channel(high.shift(1), low.shift(1), self._exit_period)

        # ATR (N) for position sizing and stops
        atr_values = atr(high, low, close, period=self._atr_period)

        current_close = int(close.iloc[-1])
        prev_close = int(close.iloc[-2])
        current_entry_upper = entry_upper.iloc[-1]
        current_long_upper = long_upper.iloc[-1]
        current_exit_lower = exit_lower.iloc[-1]
        current_atr = atr_values.iloc[-1]

        if any(pd.isna(v) for v in [current_entry_upper, current_long_upper, current_exit_lower, current_atr]):
            return []

        current_entry_upper = int(current_entry_upper)
        current_long_upper = int(current_long_upper)
        current_exit_lower = int(current_exit_lower)
        self._current_n = float(current_atr)

        signals = []

        # --- SELL signal: close drops below exit channel ---
        if current_close <= current_exit_lower:
            signals.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    action="sell",
                    strength=0.8,
                    stop_loss_cents=current_close,
                    price_cents=current_close,
                    reason=f"Turtle exit: close {current_close} <= exit channel {current_exit_lower} ({self._exit_period}d low)",
                    strategy_name=self.name,
                    indicator_data={
                        "exit_channel_lower": current_exit_lower,
                        "atr": round(self._current_n, 2),
                    },
                )
            )
            # Reset pyramid state on exit
            self._pyramid_count = 0
            self._last_entry_price = 0
            return signals

        # --- BUY signals ---
        # System 1: Short-term breakout
        system1_breakout = current_close > current_entry_upper and prev_close <= current_entry_upper
        # System 2: Long-term breakout (backup, stronger trend confirmation)
        system2_breakout = current_close > current_long_upper and prev_close <= current_long_upper

        if system1_breakout or system2_breakout:
            if self._pyramid_count == 0:
                # New entry
                stop_loss = int(current_close - self._stop_multiplier * self._current_n)
                strength = 0.9 if system2_breakout else 0.7

                sys_label = "System 2" if system2_breakout else "System 1"
                channel_val = current_long_upper if system2_breakout else current_entry_upper
                reason = (
                    f"Turtle {sys_label} breakout: close {current_close} > "
                    f"{self._entry_period}d high {channel_val}"
                )

                signals.append(
                    Signal(
                        symbol=symbol,
                        market=market,
                        action="buy",
                        strength=strength,
                        stop_loss_cents=max(1, stop_loss),
                        price_cents=current_close,
                        reason=reason,
                        strategy_name=self.name,
                        indicator_data={
                            "entry_channel_upper": current_entry_upper,
                            "long_channel_upper": current_long_upper,
                            "atr": round(self._current_n, 2),
                            "pyramid_level": 1,
                            "system": 2 if system2_breakout else 1,
                        },
                    )
                )
                self._pyramid_count = 1
                self._last_entry_price = current_close

        # --- Pyramid entries ---
        # Add if price has moved 0.5N above last entry and under max pyramids
        if self._pyramid_count > 0 and self._pyramid_count < self._max_pyramids:
            pyramid_threshold = self._last_entry_price + int(self._pyramid_atr_step * self._current_n)
            if current_close >= pyramid_threshold:
                new_stop = int(current_close - self._stop_multiplier * self._current_n)
                signals.append(
                    Signal(
                        symbol=symbol,
                        market=market,
                        action="buy",
                        strength=0.5,
                        stop_loss_cents=max(1, new_stop),
                        price_cents=current_close,
                        reason=(
                            f"Turtle pyramid #{self._pyramid_count + 1}: "
                            f"close {current_close} >= threshold {pyramid_threshold} "
                            f"(last entry + {self._pyramid_atr_step}N)"
                        ),
                        strategy_name=self.name,
                        indicator_data={
                            "entry_channel_upper": current_entry_upper,
                            "atr": round(self._current_n, 2),
                            "pyramid_level": self._pyramid_count + 1,
                            "pyramid_threshold": pyramid_threshold,
                        },
                    )
                )
                self._pyramid_count += 1
                self._last_entry_price = current_close

        return signals


class TurtleCryptoStrategy(_TurtleBase):
    """Turtle Trading adapted for crypto markets.

    Shorter channels (15/40-day) to catch faster crypto trends.
    Wider stops (2.5N) to handle crypto volatility without whipsaw.
    Max 3 pyramids (crypto fees make 4th pyramid marginal).
    8-day exit channel (faster exits due to momentum decay in crypto).
    """

    def __init__(self, **kwargs) -> None:
        defaults = {
            "entry_period": 15,
            "exit_period": 8,
            "long_entry_period": 40,
            "atr_period": 20,
            "stop_multiplier": 2.5,
            "pyramid_atr_step": 0.5,
            "max_pyramids": 3,
        }
        defaults.update(kwargs)
        super().__init__(**defaults)

    @property
    def name(self) -> str:
        return "turtle_crypto"


class TurtleStocksStrategy(_TurtleBase):
    """Classic Turtle Trading for stocks (ASX + US daily bars).

    Standard 20/55-day channels per original Turtle rules.
    Classic 2N stops.
    Max 4 pyramids at 0.5N intervals.
    10-day exit channel.
    """

    def __init__(self, **kwargs) -> None:
        defaults = {
            "entry_period": 20,
            "exit_period": 10,
            "long_entry_period": 55,
            "atr_period": 20,
            "stop_multiplier": 2.0,
            "pyramid_atr_step": 0.5,
            "max_pyramids": 4,
        }
        defaults.update(kwargs)
        super().__init__(**defaults)

    @property
    def name(self) -> str:
        return "turtle_stocks"
