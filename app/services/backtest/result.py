"""Backtest result data structures. All money in cents."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ClosedTrade:
    """A completed round-trip trade (entry + exit)."""

    symbol: str
    market: str
    entry_price_cents: int
    exit_price_cents: int
    quantity_cents: int
    pnl_cents: int
    entry_time: datetime
    exit_time: datetime
    exit_reason: str  # "signal", "stop_loss", "backtest_end"
    strategy: str
    holding_bars: int


@dataclass
class BacktestResult:
    """Complete backtest output with metrics and trade log."""

    # Config
    strategy_name: str
    symbol: str
    market: str
    timeframe: str
    start_date: str
    end_date: str
    bars_processed: int

    # Capital
    starting_cash_cents: int
    ending_cash_cents: int
    ending_equity_cents: int

    # Performance
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    max_drawdown_cents: int

    # Trade stats
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    avg_win_cents: int
    avg_loss_cents: int
    max_consecutive_wins: int
    max_consecutive_losses: int
    avg_holding_bars: float

    # Costs
    total_fees_cents: int

    # Detailed logs
    trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict for API response."""
        d = {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "market": self.market,
            "timeframe": self.timeframe,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bars_processed": self.bars_processed,
            "starting_cash_cents": self.starting_cash_cents,
            "ending_cash_cents": self.ending_cash_cents,
            "ending_equity_cents": self.ending_equity_cents,
            "total_return_pct": round(self.total_return_pct, 2),
            "annualized_return_pct": round(self.annualized_return_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "max_drawdown_cents": self.max_drawdown_cents,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2),
            "avg_win_cents": self.avg_win_cents,
            "avg_loss_cents": self.avg_loss_cents,
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "avg_holding_bars": round(self.avg_holding_bars, 1),
            "total_fees_cents": self.total_fees_cents,
            "trades": [
                {
                    "symbol": t.symbol,
                    "market": t.market,
                    "entry_price_cents": t.entry_price_cents,
                    "exit_price_cents": t.exit_price_cents,
                    "quantity_cents": t.quantity_cents,
                    "pnl_cents": t.pnl_cents,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "exit_reason": t.exit_reason,
                    "strategy": t.strategy,
                    "holding_bars": t.holding_bars,
                }
                for t in self.trades
            ],
            "equity_curve": self.equity_curve,
        }
        return d
