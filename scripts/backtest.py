"""CLI for backtesting strategies against historical data.

Usage:
    python scripts/backtest.py --strategy momentum --symbol BTC --market crypto --timeframe 1h --days 180
    python scripts/backtest.py --strategy meanrev --symbol BHP.AX --market asx --timeframe 1d --days 365
    python scripts/backtest.py --strategy auto --symbol ETH --market crypto --timeframe 4h --days 90
    python scripts/backtest.py --strategy momentum --symbol BTC --market crypto --json
"""

import argparse
import asyncio
import json
import logging
import sys

from app.services.backtest.engine import BacktestEngine
from app.services.backtest.result import BacktestResult


def format_report(result: BacktestResult) -> str:
    """Format backtest results as a readable console report."""
    lines = []
    sep = "=" * 68

    lines.append(sep)
    lines.append("  FlashTrade Backtest Report")
    lines.append(sep)
    lines.append(
        f"  Strategy:    {result.strategy_name:<16s}"
        f"Symbol:  {result.symbol} ({result.market}, {result.timeframe})"
    )
    lines.append(
        f"  Period:      {result.start_date[:10]} to {result.end_date[:10]} "
        f"({result.bars_processed} bars)"
    )
    lines.append("-" * 68)

    # Performance
    lines.append("  PERFORMANCE")
    lines.append(f"  Starting Capital:       ${result.starting_cash_cents / 100:>12,.2f}")
    lines.append(f"  Ending Equity:          ${result.ending_equity_cents / 100:>12,.2f}")

    ret_sign = "+" if result.total_return_pct >= 0 else ""
    lines.append(f"  Total Return:           {ret_sign}{result.total_return_pct:.2f}%")

    ann_sign = "+" if result.annualized_return_pct >= 0 else ""
    lines.append(f"  Annualized Return:      {ann_sign}{result.annualized_return_pct:.2f}%")
    lines.append(f"  Sharpe Ratio:           {result.sharpe_ratio:.2f}")
    lines.append(f"  Max Drawdown:           -{result.max_drawdown_pct:.2f}% (${result.max_drawdown_cents / 100:,.2f})")

    lines.append("")

    # Trade stats
    lines.append("  TRADES")
    lines.append(
        f"  Total: {result.total_trades}   "
        f"Win Rate: {result.win_rate_pct:.1f}% "
        f"({result.winning_trades}W / {result.losing_trades}L)"
    )
    lines.append(f"  Profit Factor:          {result.profit_factor:.2f}")
    lines.append(f"  Avg Win:                ${result.avg_win_cents / 100:>8,.2f}")
    lines.append(f"  Avg Loss:               ${result.avg_loss_cents / 100:>8,.2f}")
    lines.append(
        f"  Max Consecutive:        {result.max_consecutive_wins} wins, "
        f"{result.max_consecutive_losses} losses"
    )
    lines.append(f"  Avg Holding:            {result.avg_holding_bars:.1f} bars")

    lines.append("")

    # Costs
    lines.append("  COSTS")
    lines.append(f"  Total Fees Paid:        ${result.total_fees_cents / 100:>8,.2f}")

    # Trade log (last 10)
    if result.trades:
        lines.append("")
        lines.append("  RECENT TRADES (last 10)")
        lines.append(f"  {'Entry':>10} {'Exit':>10} {'P&L':>8} {'Bars':>5} {'Reason':<12}")
        for t in result.trades[-10:]:
            pnl_str = f"${t.pnl_cents / 100:+.2f}"
            lines.append(
                f"  ${t.entry_price_cents / 100:>9,.2f} "
                f"${t.exit_price_cents / 100:>9,.2f} "
                f"{pnl_str:>8} "
                f"{t.holding_bars:>5} "
                f"{t.exit_reason:<12}"
            )

    lines.append(sep)
    return "\n".join(lines)


async def run_backtest(args: argparse.Namespace) -> None:
    """Run the backtest engine and print results."""
    engine = BacktestEngine(
        strategy_name=args.strategy,
        symbol=args.symbol,
        market=args.market,
        timeframe=args.timeframe,
        days=args.days,
        auto_regime=(args.strategy == "auto"),
    )

    try:
        result = await engine.run()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_report(result))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="FlashTrade backtesting — test strategies against historical data"
    )
    parser.add_argument(
        "--strategy", required=True,
        choices=["momentum", "meanrev", "auto"],
        help="Strategy to test (auto = regime-switching)",
    )
    parser.add_argument(
        "--symbol", required=True,
        help="Symbol to backtest (e.g. BTC, ETH, BHP.AX, AAPL)",
    )
    parser.add_argument(
        "--market", required=True,
        choices=["us", "crypto", "asx"],
        help="Market the symbol belongs to",
    )
    parser.add_argument(
        "--timeframe", default="1h",
        choices=["1h", "4h", "1d"],
        help="Candle timeframe (default: 1h)",
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Lookback period in days (default: 180)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output result as JSON instead of formatted report",
    )
    args = parser.parse_args()

    asyncio.run(run_backtest(args))


if __name__ == "__main__":
    main()
