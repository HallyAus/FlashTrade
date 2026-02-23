"""CLI for trading operations.

Usage:
    python scripts/trade.py --paper    # Paper trading mode
    python scripts/trade.py --live     # LIVE trading (real money!)
    python scripts/trade.py --kill     # Emergency: close all positions

TODO: Implement in Day 5.
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="FlashTrade trading")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--paper", action="store_true", help="Paper trading mode")
    group.add_argument("--live", action="store_true", help="LIVE trading (real money!)")
    group.add_argument("--kill", action="store_true", help="Emergency: close all positions")
    args = parser.parse_args()

    if args.kill:
        print("KILL SWITCH — closing all positions... (not implemented yet)")
    elif args.live:
        print("WARNING: Live trading mode — real money at risk!")
        print("Starting live trading... (not implemented yet)")
    elif args.paper:
        print("Starting paper trading... (not implemented yet)")


if __name__ == "__main__":
    main()
