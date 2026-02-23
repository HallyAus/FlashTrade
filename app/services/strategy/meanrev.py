"""Mean reversion strategy: Bollinger Bands. Implemented in Phase 2 (Day 8)."""

from app.services.strategy.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Bollinger Band mean reversion strategy.

    Entry: Price touches lower band with RSI < 35.
    Exit: Price returns to middle band OR stop-loss.

    TODO: Implement in Day 8.
    """

    @property
    def name(self) -> str:
        return "meanrev"

    def generate_signals(self, df, symbol, market):
        # Stub — implementation in Day 8
        return []
