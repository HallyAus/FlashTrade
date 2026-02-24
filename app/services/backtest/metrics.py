"""Compute performance metrics from backtest broker state."""

import math

from app.services.backtest.broker import BacktestBroker
from app.services.backtest.result import BacktestResult, ClosedTrade

# Annualization factors (bars per year) by timeframe
BARS_PER_YEAR: dict[str, int] = {
    "1h": 8760,  # 365 * 24
    "4h": 2190,  # 365 * 6
    "1d": 365,
}


def compute_metrics(
    broker: BacktestBroker,
    strategy_name: str,
    symbol: str,
    market: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    bars_processed: int,
) -> BacktestResult:
    """Compute all metrics from completed backtest state."""
    ending_equity = broker.cash_cents  # All positions closed at this point

    total_return_pct = (
        (ending_equity - broker.starting_cash_cents) / broker.starting_cash_cents * 100
        if broker.starting_cash_cents > 0
        else 0.0
    )

    annualized_return_pct = _annualize_return(total_return_pct, bars_processed, timeframe)
    sharpe = _compute_sharpe(broker.equity_curve, timeframe)
    max_dd_pct, max_dd_cents = _compute_max_drawdown(broker.equity_curve)
    trade_stats = _compute_trade_stats(broker.closed_trades)

    return BacktestResult(
        strategy_name=strategy_name,
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        bars_processed=bars_processed,
        starting_cash_cents=broker.starting_cash_cents,
        ending_cash_cents=ending_equity,
        ending_equity_cents=ending_equity,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_cents=max_dd_cents,
        total_trades=trade_stats["total_trades"],
        winning_trades=trade_stats["winning_trades"],
        losing_trades=trade_stats["losing_trades"],
        win_rate_pct=trade_stats["win_rate_pct"],
        profit_factor=trade_stats["profit_factor"],
        avg_win_cents=trade_stats["avg_win_cents"],
        avg_loss_cents=trade_stats["avg_loss_cents"],
        max_consecutive_wins=trade_stats["max_consecutive_wins"],
        max_consecutive_losses=trade_stats["max_consecutive_losses"],
        avg_holding_bars=trade_stats["avg_holding_bars"],
        total_fees_cents=broker.total_fees_cents,
        trades=broker.closed_trades,
        equity_curve=broker.equity_curve,
    )


def _annualize_return(total_return_pct: float, bars: int, timeframe: str) -> float:
    """Annualize a total return based on the number of bars and timeframe."""
    bars_per_year = BARS_PER_YEAR.get(timeframe, 365)
    if bars <= 0:
        return 0.0
    years = bars / bars_per_year
    if years <= 0:
        return 0.0
    # Compound annualized return: (1 + total_return)^(1/years) - 1
    total_factor = 1 + total_return_pct / 100
    if total_factor <= 0:
        return -100.0
    return (total_factor ** (1 / years) - 1) * 100


def _compute_sharpe(equity_curve: list[dict], timeframe: str) -> float:
    """Annualized Sharpe ratio from equity curve snapshots.

    Sharpe = (mean_return / std_return) * sqrt(bars_per_year)
    Risk-free rate = 0.
    """
    if len(equity_curve) < 2:
        return 0.0

    # Compute per-bar returns
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity_cents"]
        curr = equity_curve[i]["equity_cents"]
        if prev > 0:
            returns.append((curr - prev) / prev)

    if not returns:
        return 0.0

    mean_ret = sum(returns) / len(returns)
    if len(returns) < 2:
        return 0.0
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    std_ret = math.sqrt(variance)

    if std_ret == 0:
        return 0.0

    bars_per_year = BARS_PER_YEAR.get(timeframe, 365)
    return (mean_ret / std_ret) * math.sqrt(bars_per_year)


def _compute_max_drawdown(equity_curve: list[dict]) -> tuple[float, int]:
    """Max drawdown as (percentage, absolute_cents) from equity curve."""
    if not equity_curve:
        return 0.0, 0

    peak = equity_curve[0]["equity_cents"]
    max_dd_pct = 0.0
    max_dd_cents = 0

    for point in equity_curve:
        equity = point["equity_cents"]
        if equity > peak:
            peak = equity

        if peak > 0:
            dd_cents = peak - equity
            dd_pct = dd_cents / peak * 100

            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_cents = dd_cents

    return max_dd_pct, max_dd_cents


def _compute_trade_stats(trades: list[ClosedTrade]) -> dict:
    """Win rate, profit factor, avg win/loss, consecutive streaks."""
    if not trades:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "avg_win_cents": 0,
            "avg_loss_cents": 0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "avg_holding_bars": 0.0,
        }

    wins = [t for t in trades if t.pnl_cents > 0]
    losses = [t for t in trades if t.pnl_cents <= 0]

    gross_profit = sum(t.pnl_cents for t in wins)
    gross_loss = abs(sum(t.pnl_cents for t in losses))

    # Consecutive streaks
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for t in trades:
        if t.pnl_cents > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    total_holding = sum(t.holding_bars for t in trades)

    return {
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else 9999.0 if gross_profit > 0 else 0.0,
        "avg_win_cents": int(gross_profit / len(wins)) if wins else 0,
        "avg_loss_cents": int(-gross_loss / len(losses)) if losses else 0,
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "avg_holding_bars": total_holding / len(trades) if trades else 0.0,
    }
