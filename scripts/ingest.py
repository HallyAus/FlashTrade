"""CLI for data ingestion.

Usage:
    python scripts/ingest.py --backfill 6m   # Backfill 6 months OHLCV
    python scripts/ingest.py --backfill 1y   # Backfill 1 year
    python scripts/ingest.py --live           # Run one cycle of live data pulls
"""

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def do_backfill(period: str) -> None:
    """Run historical backfill for all markets."""
    from app.services.data.ingestion import backfill_all

    logger.info("Starting backfill for period: %s", period)
    results = await backfill_all(period)

    print("\n=== Backfill Results ===")
    total = 0
    for key, count in results.items():
        print(f"  {key}: {count:,} rows")
        total += count
    print(f"  TOTAL: {total:,} rows")
    print("========================\n")


async def do_live_pull() -> None:
    """Run one cycle of live data ingestion for all markets."""
    from app.services.data.ingestion import ingest_crypto_ohlcv, ingest_stock_ohlcv

    logger.info("Running live data pull...")

    crypto = await ingest_crypto_ohlcv(timeframe="1h", limit=5)
    asx = await ingest_stock_ohlcv("asx", timeframe="1d", period="5d")
    us = await ingest_stock_ohlcv("us", timeframe="1d", period="5d")

    print(f"\nLive pull: crypto={crypto}, asx={asx}, us={us} rows ingested\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="FlashTrade data ingestion")
    parser.add_argument(
        "--backfill",
        type=str,
        help="Backfill period (e.g., 6m for 6 months, 1y for 1 year)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run one cycle of live data pulls",
    )
    args = parser.parse_args()

    if args.backfill:
        # Normalize: "6m" -> "6mo" for yfinance compatibility
        period = args.backfill
        if period.endswith("m") and not period.endswith("mo"):
            period = period + "o"
        asyncio.run(do_backfill(period))
    elif args.live:
        asyncio.run(do_live_pull())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
