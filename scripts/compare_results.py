"""Compare batch backtest results across strategies.

Usage:
    python scripts/compare_results.py results/day7-*.json results/day8-*.json
    python scripts/compare_results.py results/day7-momentum_*.json results/day9-tuned_*.json
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def load_results(paths: list[str]) -> list[dict]:
    """Load and merge results from multiple JSON files."""
    all_results = []
    for p in paths:
        with open(p) as f:
            data = json.load(f)
        label = data.get("label", Path(p).stem)
        params = data.get("params", {})
        for r in data["results"]:
            r["source_label"] = label
            r["source_params"] = params
            all_results.append(r)
    return all_results


def format_comparison(results: list[dict], paths: list[str]) -> str:
    """Format head-to-head comparison as markdown."""
    lines = []
    lines.append("# Strategy Comparison Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Sources: {', '.join(Path(p).name for p in paths)}")
    lines.append("")

    # Group by (symbol, market, timeframe)
    grouped = defaultdict(list)
    for r in results:
        key = (r["symbol"], r["market"], r["timeframe"])
        grouped[key].append(r)

    # Per-symbol comparison table
    lines.append("## Per-Symbol Comparison")
    lines.append("")
    lines.append(
        "| Symbol | Market | TF | Strategy | Label | Return% | Sharpe | MaxDD% | Trades | Winner |"
    )
    lines.append(
        "|--------|--------|----|----------|-------|---------|--------|--------|--------|--------|"
    )

    # Track wins per strategy per market
    market_wins = defaultdict(lambda: defaultdict(int))
    strategy_returns = defaultdict(list)

    for key in sorted(grouped.keys()):
        entries = grouped[key]
        symbol, market, tf = key

        # Find best by Sharpe (primary metric)
        valid = [e for e in entries if not e.get("error")]
        if not valid:
            for e in entries:
                lines.append(
                    f"| {symbol} | {market} | {tf} | "
                    f"{e.get('strategy', '?')} | {e.get('source_label', '?')} | "
                    f"ERROR | - | - | - | - |"
                )
            continue

        best = max(valid, key=lambda e: e.get("sharpe_ratio", -999))

        for e in valid:
            is_winner = (e is best and len(valid) > 1)
            winner_mark = ">>>" if is_winner else ""
            ret_str = f"{e['total_return_pct']:+.2f}"
            strategy = e.get("strategy", "?")

            lines.append(
                f"| {symbol} | {market} | {tf} | "
                f"{strategy} | {e.get('source_label', '?')} | "
                f"{ret_str} | {e.get('sharpe_ratio', 0):.2f} | "
                f"{e.get('max_drawdown_pct', 0):.2f} | "
                f"{e.get('total_trades', 0)} | {winner_mark} |"
            )

            strategy_returns[strategy].append(e.get("total_return_pct", 0))

            if is_winner:
                market_wins[market][strategy] += 1

    # Per-market summary
    lines.append("")
    lines.append("## Per-Market Summary (wins by Sharpe ratio)")
    lines.append("")

    for market in sorted(market_wins.keys()):
        wins = market_wins[market]
        total = sum(wins.values())
        parts = [f"{strat} wins {count}/{total}" for strat, count in sorted(wins.items(), key=lambda x: -x[1])]
        lines.append(f"- **{market}**: {', '.join(parts)}")

    # Overall strategy stats
    lines.append("")
    lines.append("## Overall Strategy Performance")
    lines.append("")
    lines.append("| Strategy | Avg Return% | Median Return% | Runs |")
    lines.append("|----------|-------------|----------------|------|")

    for strat in sorted(strategy_returns.keys()):
        returns = strategy_returns[strat]
        avg_ret = sum(returns) / len(returns) if returns else 0
        sorted_rets = sorted(returns)
        median_ret = sorted_rets[len(sorted_rets) // 2] if sorted_rets else 0
        lines.append(
            f"| {strat} | {avg_ret:+.2f} | {median_ret:+.2f} | {len(returns)} |"
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare batch backtest results across strategies"
    )
    parser.add_argument(
        "files", nargs="+",
        help="JSON result files to compare",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: results/comparison_{timestamp}.md)",
    )
    args = parser.parse_args()

    # Expand globs (shell may not expand on Windows)
    expanded = []
    for pattern in args.files:
        matches = list(Path(".").glob(pattern))
        if matches:
            expanded.extend(str(m) for m in matches)
        elif Path(pattern).exists():
            expanded.append(pattern)
        else:
            print(f"Warning: no files matching '{pattern}'", file=sys.stderr)

    if not expanded:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    print(f"Comparing {len(expanded)} result files...")
    results = load_results(expanded)
    print(f"Loaded {len(results)} individual results")

    report = format_comparison(results, expanded)

    if args.output:
        out_path = Path(args.output)
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path("results") / f"comparison_{timestamp}.md"

    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        f.write(report)

    print(f"\nReport saved: {out_path}")
    print()
    # Print to stdout too
    print(report)


if __name__ == "__main__":
    main()
