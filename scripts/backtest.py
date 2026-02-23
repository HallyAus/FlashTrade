"""CLI for backtesting strategies.

Usage:
    python scripts/backtest.py --strategy momentum --market all
    python scripts/backtest.py --strategy meanrev --market crypto

TODO: Implement in Day 6.
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="FlashTrade backtesting")
    parser.add_argument("--strategy", type=str, required=True, choices=["momentum", "meanrev"])
    parser.add_argument("--market", type=str, required=True, choices=["us", "crypto", "asx", "all"])
    parser.add_argument("--timeframe", type=str, default="4h", choices=["1h", "4h", "1d"])
    args = parser.parse_args()

    print(f"Backtesting {args.strategy} on {args.market} ({args.timeframe})... (not implemented yet)")


if __name__ == "__main__":
    main()
