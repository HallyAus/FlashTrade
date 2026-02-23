"""Momentum strategy: RSI + MACD crossover. Implemented in Phase 2 (Day 7)."""

from app.services.strategy.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """RSI + MACD crossover momentum strategy.

    Entry: RSI crosses above 30 AND MACD crosses signal line.
    Exit: RSI crosses above 70 OR trailing stop hit.

    TODO: Implement in Day 7.
    """

    @property
    def name(self) -> str:
        return "momentum"

    def generate_signals(self, df, symbol, market):
        # Stub — implementation in Day 7
        return []
