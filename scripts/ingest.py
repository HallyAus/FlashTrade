"""CLI for data ingestion.

Usage:
    python scripts/ingest.py --backfill 6m   # Backfill 6 months OHLCV
    python scripts/ingest.py --live           # Start live data feed

TODO: Implement in Day 2.
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="FlashTrade data ingestion")
    parser.add_argument("--backfill", type=str, help="Backfill period (e.g., 6m, 1y)")
    parser.add_argument("--live", action="store_true", help="Start live data feed")
    args = parser.parse_args()

    if args.backfill:
        print(f"Backfilling {args.backfill} of data... (not implemented yet)")
    elif args.live:
        print("Starting live data feed... (not implemented yet)")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
