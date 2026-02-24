"""Backtesting engine for FlashTrade strategies.

Custom walk-forward simulator that calls existing strategies directly.
No third-party backtesting framework needed — tests production code paths.
"""

from app.services.backtest.engine import BacktestEngine
from app.services.backtest.result import BacktestResult, ClosedTrade

__all__ = ["BacktestEngine", "BacktestResult", "ClosedTrade"]
