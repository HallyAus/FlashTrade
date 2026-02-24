"""Batch backtesting — run all symbol/strategy/timeframe combinations.

Usage:
    python scripts/batch_backtest.py --strategy momentum --days 180 --label day7-momentum
    python scripts/batch_backtest.py --strategy all --days 180 --label day8-all
    python scripts/batch_backtest.py --strategy momentum --market-filter crypto --days 60
    python scripts/batch_backtest.py --strategy meanrev --params '{"rsi_oversold":30}' --label day9-tuned
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.services.backtest.engine import BacktestEngine
from app.services.backtest.result import BacktestResult

logger = logging.getLogger(__name__)

# Backtest matrix — same symbols as WATCHED_SYMBOLS in auto_trader.py
BACKTEST_MATRIX = [
    # Crypto: 10 symbols × [1h, 1d] (no 4h data backfilled)
    *[
        {"symbol": sym, "market": "crypto", "timeframes": ["1h", "1d"]}
        for sym in ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "POL"]
    ],
    # ASX: 10 symbols × [1d] only (yfinance lacks reliable intraday)
    *[
        {"symbol": sym, "market": "asx", "timeframes": ["1d"]}
        for sym in [
            "BHP.AX", "CBA.AX", "CSL.AX", "WDS.AX", "FMG.AX",
            "NAB.AX", "WBC.AX", "ANZ.AX", "WOW.AX", "RIO.AX",
        ]
    ],
    # US: 10 symbols × [1d] only
    *[
        {"symbol": sym, "market": "us", "timeframes": ["1d"]}
        for sym in ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AMD", "NFLX", "QQQ"]
    ],
]


def expand_matrix(market_filter: str | None = None) -> list[dict]:
    """Expand the matrix into individual (symbol, market, timeframe) jobs."""
    jobs = []
    for entry in BACKTEST_MATRIX:
        if market_filter and entry["market"] != market_filter:
            continue
        for tf in entry["timeframes"]:
            jobs.append({
                "symbol": entry["symbol"],
                "market": entry["market"],
                "timeframe": tf,
            })
    return jobs


def format_summary_table(results: list[dict]) -> str:
    """Format results as a markdown summary table."""
    lines = []
    lines.append("# Batch Backtest Results")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append(
        "| Symbol | Market | TF | Strategy | Return% | Sharpe | MaxDD% | WinRate% | Trades | PF |"
    )
    lines.append(
        "|--------|--------|----|----------|---------|--------|--------|----------|--------|----|"
    )

    total_return = 0.0
    sharpe_values = []
    worst_dd = 0.0
    total_trades = 0
    success_count = 0
    error_count = 0

    for r in results:
        if r.get("error"):
            lines.append(
                f"| {r['symbol']} | {r['market']} | {r['timeframe']} | "
                f"{r['strategy']} | ERROR | - | - | - | - | - |"
            )
            error_count += 1
            continue

        success_count += 1
        ret = r["total_return_pct"]
        sharpe = r["sharpe_ratio"]
        dd = r["max_drawdown_pct"]
        wr = r["win_rate_pct"]
        trades = r["total_trades"]
        pf = r["profit_factor"]

        total_return += ret
        sharpe_values.append(sharpe)
        worst_dd = max(worst_dd, dd)
        total_trades += trades

        ret_str = f"{ret:+.2f}"
        lines.append(
            f"| {r['symbol']} | {r['market']} | {r['timeframe']} | "
            f"{r['strategy']} | {ret_str} | {sharpe:.2f} | {dd:.2f} | "
            f"{wr:.1f} | {trades} | {pf:.2f} |"
        )

    # Aggregate summary
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if success_count > 0:
        avg_return = total_return / success_count
        median_sharpe = sorted(sharpe_values)[len(sharpe_values) // 2] if sharpe_values else 0
        lines.append(f"- **Runs**: {success_count} successful, {error_count} errors")
        lines.append(f"- **Avg Return**: {avg_return:+.2f}%")
        lines.append(f"- **Median Sharpe**: {median_sharpe:.2f}")
        lines.append(f"- **Worst Drawdown**: {worst_dd:.2f}%")
        lines.append(f"- **Total Trades**: {total_trades}")
    else:
        lines.append("- No successful runs.")

    return "\n".join(lines)


async def run_batch(args: argparse.Namespace) -> None:
    """Run batch backtests across the matrix."""
    strategies = (
        ["momentum", "meanrev", "auto"] if args.strategy == "all"
        else [args.strategy]
    )

    strategy_params = {}
    if args.params:
        strategy_params = json.loads(args.params)

    jobs = expand_matrix(args.market_filter)
    total_jobs = len(jobs) * len(strategies)

    if total_jobs == 0:
        print("No jobs to run. Check --market-filter.", file=sys.stderr)
        sys.exit(1)

    print(f"Batch backtest: {total_jobs} runs ({len(jobs)} symbols × {len(strategies)} strategies)")
    print(f"Days: {args.days}, Params: {strategy_params or 'defaults'}")
    print()

    all_results = []
    completed = 0
    batch_start = time.time()

    for strategy_name in strategies:
        for job in jobs:
            completed += 1
            pct = completed * 100 // total_jobs
            label = f"[{completed}/{total_jobs}] {pct}% {strategy_name} {job['symbol']} {job['market']} {job['timeframe']}"

            t0 = time.time()
            try:
                # Extract engine-level params from strategy_params
                engine_kwargs = {}
                strat_params = dict(strategy_params) if strategy_name != "auto" else {}
                for key in ("fee_tier", "cooldown_bars"):
                    if key in strat_params:
                        engine_kwargs[key] = strat_params.pop(key)

                engine = BacktestEngine(
                    strategy_name=strategy_name if strategy_name != "auto" else "meanrev",
                    symbol=job["symbol"],
                    market=job["market"],
                    timeframe=job["timeframe"],
                    days=args.days,
                    auto_regime=(strategy_name == "auto"),
                    strategy_params=strat_params,
                    **engine_kwargs,
                )
                result = await engine.run()
                elapsed = time.time() - t0

                row = {
                    "symbol": job["symbol"],
                    "market": job["market"],
                    "timeframe": job["timeframe"],
                    "strategy": result.strategy_name,
                    "total_return_pct": result.total_return_pct,
                    "annualized_return_pct": result.annualized_return_pct,
                    "sharpe_ratio": result.sharpe_ratio,
                    "max_drawdown_pct": result.max_drawdown_pct,
                    "max_drawdown_cents": result.max_drawdown_cents,
                    "total_trades": result.total_trades,
                    "winning_trades": result.winning_trades,
                    "losing_trades": result.losing_trades,
                    "win_rate_pct": result.win_rate_pct,
                    "profit_factor": result.profit_factor,
                    "avg_win_cents": result.avg_win_cents,
                    "avg_loss_cents": result.avg_loss_cents,
                    "avg_holding_bars": result.avg_holding_bars,
                    "total_fees_cents": result.total_fees_cents,
                    "bars_processed": result.bars_processed,
                    "start_date": result.start_date,
                    "end_date": result.end_date,
                    "starting_cash_cents": result.starting_cash_cents,
                    "ending_equity_cents": result.ending_equity_cents,
                }

                ret_str = f"{result.total_return_pct:+.2f}%"
                print(f"{label} ... {ret_str} ({elapsed:.1f}s)")
                all_results.append(row)

            except (ValueError, Exception) as e:
                elapsed = time.time() - t0
                print(f"{label} ... ERROR: {e} ({elapsed:.1f}s)")
                all_results.append({
                    "symbol": job["symbol"],
                    "market": job["market"],
                    "timeframe": job["timeframe"],
                    "strategy": strategy_name,
                    "error": str(e),
                })

    batch_elapsed = time.time() - batch_start
    print(f"\nBatch complete: {len(all_results)} runs in {batch_elapsed:.1f}s")

    # Save results
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    label = args.label or f"batch_{args.strategy}"
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    json_path = results_dir / f"{label}_{timestamp}.json"
    md_path = results_dir / f"{label}_{timestamp}.md"

    with open(json_path, "w") as f:
        json.dump({
            "label": label,
            "strategy": args.strategy,
            "days": args.days,
            "params": strategy_params,
            "timestamp": timestamp,
            "results": all_results,
        }, f, indent=2)

    with open(md_path, "w") as f:
        f.write(format_summary_table(all_results))

    print(f"JSON: {json_path}")
    print(f"Summary: {md_path}")


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Batch backtesting — run strategies across all symbols/timeframes"
    )
    parser.add_argument(
        "--strategy", required=True,
        choices=["momentum", "meanrev", "auto", "all"],
        help="Strategy to test (all = run momentum, meanrev, and auto)",
    )
    parser.add_argument(
        "--market-filter",
        choices=["crypto", "asx", "us"],
        help="Only run symbols from this market",
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Lookback period in days (default: 180)",
    )
    parser.add_argument(
        "--label",
        help="Label for output files (default: batch_{strategy})",
    )
    parser.add_argument(
        "--params",
        help='Strategy params as JSON string, e.g. \'{"rsi_entry":33}\'',
    )
    args = parser.parse_args()

    asyncio.run(run_batch(args))


if __name__ == "__main__":
    main()
