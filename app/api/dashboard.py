"""Dashboard API routes — portfolio overview, live prices, health, data quality."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from app.services.data.ccxt_feed import CCXTFeed
from app.services.data.market_calendar import market_status_summary
from app.services.data.yfinance_feed import YFinanceFeed

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Module-level singletons so the in-memory cache persists across requests
_ccxt_feed = CCXTFeed()
_yfinance_feed = YFinanceFeed()


@router.get("/portfolio")
async def get_portfolio():
    """Get portfolio overview with positions and P&L."""
    return {
        "portfolio_value_cents": 1_000_000,
        "daily_pnl_cents": 0,
        "positions": [],
        "status": "paper_trading",
    }


@router.get("/health")
async def health_check():
    """System health status."""
    return {"status": "ok", "trading_mode": "paper"}


@router.get("/prices")
async def get_live_prices():
    """Get live prices for crypto, ASX, and US stocks.

    Crypto: via CCXT public API (~real-time)
    Stocks: via yfinance (free, 15-min delayed)
    All prices in cents. Cached in-memory (60s crypto, 120s stocks).
    """
    crypto_prices = []
    stock_prices = []
    errors = []

    try:
        crypto_raw = await _ccxt_feed.get_prices()
        crypto_prices = [
            {
                "symbol": p.symbol,
                "market": "crypto",
                "price_cents": p.price_cents,
                "currency": p.currency,
                "change_24h_pct": p.change_24h_pct,
                "bid_cents": p.bid_cents,
                "ask_cents": p.ask_cents,
                "volume_24h": p.volume_24h,
                "timestamp_utc": p.timestamp_utc,
                "delayed": False,
            }
            for p in crypto_raw
        ]
    except Exception as e:
        logger.error("Crypto price fetch failed: %s", e)
        errors.append({"source": "crypto", "error": str(e)})

    try:
        stock_raw = await _yfinance_feed.get_prices()
        stock_prices = [
            {
                "symbol": p.symbol,
                "market": p.market,
                "price_cents": p.price_cents,
                "currency": p.currency,
                "change_pct": p.change_pct,
                "previous_close_cents": p.previous_close_cents,
                "timestamp_utc": p.timestamp_utc,
                "delayed": p.delayed,
            }
            for p in stock_raw
        ]
    except Exception as e:
        logger.error("Stock price fetch failed: %s", e)
        errors.append({"source": "stocks", "error": str(e)})

    return {
        "crypto": crypto_prices,
        "stocks": stock_prices,
        "errors": errors,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/market-status")
async def get_market_status():
    """Get open/closed status for all markets with session times."""
    return {
        "markets": market_status_summary(),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/data-quality")
async def get_data_quality():
    """Run data quality checks and return report.

    Checks: missing candles, stale data, price outliers.
    """
    from app.services.data.quality import run_quality_checks

    try:
        report = await run_quality_checks(lookback_hours=168)
        return {
            "checked_at_utc": report.checked_at_utc,
            "total_issues": report.total_issues,
            "errors": report.errors,
            "warnings": report.warnings,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "market": s.market,
                    "timeframe": s.timeframe,
                    "total_rows": s.total_rows,
                    "expected_rows": s.expected_rows,
                    "missing_pct": s.missing_pct,
                    "latest_candle_utc": s.latest_candle_utc,
                    "staleness_minutes": s.staleness_minutes,
                    "outlier_count": s.outlier_count,
                    "issues": [
                        {
                            "severity": i.severity,
                            "check": i.check,
                            "message": i.message,
                        }
                        for i in s.issues
                    ],
                }
                for s in report.symbols
            ],
        }
    except Exception as e:
        logger.error("Data quality check failed: %s", e)
        return {"error": str(e), "total_issues": -1}
