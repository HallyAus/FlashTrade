"""One-at-a-time parameter sweep for strategy optimization.

Varies one parameter at a time while holding others at defaults.
Uses Sharpe ratio as the primary optimization target.

Usage:
    python scripts/param_sweep.py --strategy momentum --symbols BTC,BHP.AX,AAPL --days 180
    python scripts/param_sweep.py --strategy meanrev --symbols ETH,CBA.AX,NVDA --days 180
    python scripts/param_sweep.py --strategy both --symbols BTC,BHP.AX,AAPL --days 180
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

logger = logging.getLogger(__name__)

# Symbol → (market, timeframe) mapping for convenience
SYMBOL_INFO = {
    # Crypto
    "BTC": ("crypto", "1h"), "ETH": ("crypto", "1h"), "SOL": ("crypto", "1h"),
    "XRP": ("crypto", "1h"), "DOGE": ("crypto", "1h"), "ADA": ("crypto", "1h"),
    "AVAX": ("crypto", "1h"), "LINK": ("crypto", "1h"), "DOT": ("crypto", "1h"),
    "POL": ("crypto", "1h"),
    # ASX
    "BHP.AX": ("asx", "1d"), "CBA.AX": ("asx", "1d"), "CSL.AX": ("asx", "1d"),
    "WDS.AX": ("asx", "1d"), "FMG.AX": ("asx", "1d"), "NAB.AX": ("asx", "1d"),
    "WBC.AX": ("asx", "1d"), "ANZ.AX": ("asx", "1d"), "WOW.AX": ("asx", "1d"),
    "RIO.AX": ("asx", "1d"),
    # US
    "AAPL": ("us", "1d"), "NVDA": ("us", "1d"), "MSFT": ("us", "1d"),
    "GOOGL": ("us", "1d"), "AMZN": ("us", "1d"), "META": ("us", "1d"),
    "TSLA": ("us", "1d"), "AMD": ("us", "1d"), "NFLX": ("us", "1d"),
    "QQQ": ("us", "1d"),
}

# Parameter sweep ranges — one-at-a-time (not grid search)
MOMENTUM_SWEEPS = {
    "rsi_entry": [25, 28, 30, 33, 35],
    "rsi_exit": [65, 68, 70, 73, 75],
    "atr_stop_multiplier": [1.5, 1.75, 2.0, 2.5, 3.0],
}

MEANREV_SWEEPS = {
    "rsi_oversold": [25, 30, 35, 40],
    "rsi_overbought": [60, 65, 70, 75],
    "bb_std": [1.5, 1.75, 2.0, 2.5],
    "atr_stop_multiplier": [1.0, 1.5, 2.0],
}

# Default values (matching strategy constructors)
MOMENTUM_DEFAULTS = {"rsi_entry": 30, "rsi_exit": 70, "atr_stop_multiplier": 2.0}
MEANREV_DEFAULTS = {"rsi_oversold": 35, "rsi_overbought": 65, "bb_std": 2.0, "atr_stop_multiplier": 1.5}


async def run_single(
    strategy_name: str, symbol: str, market: str, timeframe: str,
    days: int, params: dict,
) -> dict | None:
    """Run a single backtest, return summary or None on error."""
    try:
        engine = BacktestEngine(
            strategy_name=strategy_name,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            days=days,
            strategy_params=params,
        )
        result = await engine.run()
        return {
            "sharpe_ratio": result.sharpe_ratio,
            "total_return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_trades": result.total_trades,
            "win_rate_pct": result.win_rate_pct,
            "profit_factor": result.profit_factor,
        }
    except (ValueError, Exception) as e:
        logger.warning("Error: %s %s %s: %s", strategy_name, symbol, params, e)
        return None


async def sweep_strategy(
    strategy_name: str,
    sweeps: dict,
    defaults: dict,
    symbols: list[str],
    days: int,
) -> list[dict]:
    """Run one-at-a-time sweep for a strategy."""
    all_sweep_results = []

    # Count total runs for progress
    total_runs = sum(len(values) for values in sweeps.values()) * len(symbols)
    completed = 0

    for param_name, param_values in sweeps.items():
        print(f"\n--- Sweeping {strategy_name}.{param_name} ---")
        param_results = []

        for value in param_values:
            # Build params: defaults + override this one param
            params = {**defaults, param_name: value}
            is_default = (value == defaults[param_name])
            tag = " (default)" if is_default else ""

            sharpe_values = []
            return_values = []

            for symbol in symbols:
                completed += 1
                info = SYMBOL_INFO.get(symbol)
                if not info:
                    print(f"  Unknown symbol: {symbol}", file=sys.stderr)
                    continue

                market, timeframe = info
                result = await run_single(
                    strategy_name, symbol, market, timeframe, days, params,
                )
                if result:
                    sharpe_values.append(result["sharpe_ratio"])
                    return_values.append(result["total_return_pct"])

            if sharpe_values:
                avg_sharpe = sum(sharpe_values) / len(sharpe_values)
                avg_return = sum(return_values) / len(return_values)
            else:
                avg_sharpe = float("nan")
                avg_return = float("nan")

            print(
                f"  {param_name}={value}{tag}: "
                f"avg_sharpe={avg_sharpe:.3f}, avg_return={avg_return:+.2f}%, "
                f"({len(sharpe_values)}/{len(symbols)} symbols)"
            )

            row = {
                "param_name": param_name,
                "param_value": value,
                "is_default": is_default,
                "avg_sharpe": round(avg_sharpe, 4),
                "avg_return_pct": round(avg_return, 2),
                "individual_sharpes": [round(s, 3) for s in sharpe_values],
                "n_symbols": len(sharpe_values),
            }
            param_results.append(row)

        # Identify best value for this param
        valid = [r for r in param_results if r["n_symbols"] > 0]
        if valid:
            best = max(valid, key=lambda r: r["avg_sharpe"])
            print(
                f"  >>> Best {param_name}: {best['param_value']} "
                f"(Sharpe {best['avg_sharpe']:.3f})"
            )

        all_sweep_results.extend(param_results)

    return all_sweep_results


def format_sweep_report(
    strategy_name: str,
    results: list[dict],
    symbols: list[str],
    defaults: dict,
) -> str:
    """Format sweep results as markdown."""
    lines = []
    lines.append(f"# Parameter Sweep: {strategy_name}")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Symbols: {', '.join(symbols)}")
    lines.append(f"Defaults: {json.dumps(defaults)}")
    lines.append("")

    # Group by param_name
    by_param = {}
    for r in results:
        by_param.setdefault(r["param_name"], []).append(r)

    for param_name, rows in by_param.items():
        lines.append(f"## {param_name}")
        lines.append("")
        lines.append("| Value | Avg Sharpe | Avg Return% | N | Default |")
        lines.append("|-------|-----------|-------------|---|---------|")

        valid = [r for r in rows if r["n_symbols"] > 0]
        best_sharpe = max((r["avg_sharpe"] for r in valid), default=0)

        for r in rows:
            marker = " **best**" if r["avg_sharpe"] == best_sharpe and r["n_symbols"] > 0 else ""
            default_mark = "yes" if r["is_default"] else ""
            lines.append(
                f"| {r['param_value']} | {r['avg_sharpe']:.3f}{marker} | "
                f"{r['avg_return_pct']:+.2f} | {r['n_symbols']} | {default_mark} |"
            )

        lines.append("")

    # Recommended params
    lines.append("## Recommended Parameters")
    lines.append("")
    recommended = dict(defaults)
    for param_name, rows in by_param.items():
        valid = [r for r in rows if r["n_symbols"] > 0]
        if valid:
            best = max(valid, key=lambda r: r["avg_sharpe"])
            recommended[param_name] = best["param_value"]
            change = "" if best["is_default"] else f" (changed from {defaults[param_name]})"
            lines.append(f"- **{param_name}**: {best['param_value']}{change}")

    lines.append("")
    lines.append(f"```json\n{json.dumps(recommended, indent=2)}\n```")

    return "\n".join(lines)


async def run_sweep(args: argparse.Namespace) -> None:
    """Run the parameter sweep."""
    symbols = [s.strip() for s in args.symbols.split(",")]
    strategies = (
        ["momentum", "meanrev"] if args.strategy == "both"
        else [args.strategy]
    )

    # Validate symbols
    for sym in symbols:
        if sym not in SYMBOL_INFO:
            print(f"Unknown symbol: {sym}. Known: {', '.join(sorted(SYMBOL_INFO.keys()))}", file=sys.stderr)
            sys.exit(1)

    t0 = time.time()

    for strategy_name in strategies:
        if strategy_name == "momentum":
            sweeps, defaults = MOMENTUM_SWEEPS, MOMENTUM_DEFAULTS
        else:
            sweeps, defaults = MEANREV_SWEEPS, MEANREV_DEFAULTS

        print(f"\n{'='*60}")
        print(f"Parameter Sweep: {strategy_name}")
        print(f"Symbols: {', '.join(symbols)}")
        print(f"Defaults: {defaults}")
        print(f"{'='*60}")

        results = await sweep_strategy(
            strategy_name, sweeps, defaults, symbols, args.days,
        )

        # Save results
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        results_dir = Path("results")
        results_dir.mkdir(exist_ok=True)

        json_path = results_dir / f"sweep_{strategy_name}_{timestamp}.json"
        md_path = results_dir / f"sweep_{strategy_name}_{timestamp}.md"

        with open(json_path, "w") as f:
            json.dump({
                "strategy": strategy_name,
                "symbols": symbols,
                "days": args.days,
                "defaults": defaults,
                "sweeps": {k: list(v) for k, v in sweeps.items()},
                "timestamp": timestamp,
                "results": results,
            }, f, indent=2)

        report = format_sweep_report(strategy_name, results, symbols, defaults)
        with open(md_path, "w") as f:
            f.write(report)

        print(f"\nJSON: {json_path}")
        print(f"Report: {md_path}")
        print()
        print(report)

    elapsed = time.time() - t0
    print(f"\nSweep complete in {elapsed:.1f}s")


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="One-at-a-time parameter sweep for strategy optimization"
    )
    parser.add_argument(
        "--strategy", required=True,
        choices=["momentum", "meanrev", "both"],
        help="Strategy to sweep (both = run momentum and meanrev)",
    )
    parser.add_argument(
        "--symbols", required=True,
        help="Comma-separated list of symbols (e.g. BTC,BHP.AX,AAPL)",
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Lookback period in days (default: 180)",
    )
    args = parser.parse_args()

    asyncio.run(run_sweep(args))


if __name__ == "__main__":
    main()
