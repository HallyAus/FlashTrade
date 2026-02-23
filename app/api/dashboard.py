"""Dashboard API routes — portfolio overview, live prices, health, data quality, charts."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.database import async_session
from app.models.ohlcv import OHLCV
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
    """Get portfolio overview with positions, cash, and P&L.

    Starting cash: $10,000 AUD (1,000,000 cents).
    Cash decreases when buying, increases when selling.
    Unrealized P&L calculated from live prices, not stale DB values.
    """
    from app.services.execution.paper_executor import PaperExecutor
    from app.api.admin import risk_manager

    STARTING_CASH_CENTS = 1_000_000  # $10,000 AUD

    executor = PaperExecutor(risk_manager)
    positions = await executor.get_positions()

    # Fetch live prices to calculate real unrealized P&L
    live_prices = {}
    try:
        crypto_raw = await _ccxt_feed.get_prices()
        for p in crypto_raw:
            live_prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.warning("Failed to fetch crypto prices for portfolio: %s", e)
    try:
        stock_raw = await _yfinance_feed.get_prices()
        for p in stock_raw:
            live_prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.warning("Failed to fetch stock prices for portfolio: %s", e)

    # Update positions with live prices and calculate unrealized P&L
    for pos in positions:
        symbol = pos["symbol"]
        live_price = live_prices.get(symbol)
        if live_price:
            pos["current_price_cents"] = live_price
            # P&L = (current - entry) * quantity / entry
            # quantity is in cents (dollar amount invested), so P&L = quantity * (current - entry) / entry
            entry = pos["entry_price_cents"]
            if entry > 0:
                pos["unrealized_pnl_cents"] = int(
                    pos["quantity"] * (live_price - entry) / entry
                )

    # Get all filled trades
    async with async_session() as session:
        from app.models.trade import Trade
        result = await session.execute(
            select(Trade).where(Trade.status == "filled")
        )
        trades = result.scalars().all()

    # Cash = starting cash - buys + sells
    cash_cents = STARTING_CASH_CENTS
    for t in trades:
        if t.side == "buy":
            cash_cents -= t.quantity_cents
        elif t.side == "sell":
            cash_cents += t.quantity_cents

    # Portfolio value = current market value of open positions
    positions_value_cents = 0
    for pos in positions:
        entry = pos["entry_price_cents"]
        current = pos["current_price_cents"]
        qty = pos["quantity"]
        # Value = quantity * (current / entry) — position worth at current price
        if entry > 0:
            positions_value_cents += int(qty * current / entry)
        else:
            positions_value_cents += qty

    # Unrealized P&L = sum of all position P&Ls (already calculated above from live prices)
    unrealized_pnl_cents = sum(p.get("unrealized_pnl_cents", 0) for p in positions)

    # Realized P&L = sum of profit/loss from closed (sell) trades
    # For each sell trade, the P&L is embedded in the reason field by the executor.
    # More accurately: cash_now + cost_of_open_positions - starting_cash
    # This equals the net gain/loss from all fully closed positions.
    cost_basis_open = sum(p.get("quantity", 0) for p in positions)
    realized_pnl_cents = cash_cents + cost_basis_open - STARTING_CASH_CENTS

    return {
        "cash_cents": cash_cents,
        "positions_value_cents": positions_value_cents,
        "unrealized_pnl_cents": unrealized_pnl_cents,
        "realized_pnl_cents": realized_pnl_cents,
        "starting_cash_cents": STARTING_CASH_CENTS,
        "positions": positions,
        "open_positions_count": len(positions),
        "total_trades": len(trades),
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


@router.get("/chart")
async def get_chart_data(
    symbol: str = Query(..., description="e.g. BTC, SPY, BHP.AX"),
    timeframe: str = Query("1h", description="1m, 5m, 1h, 4h, 1d"),
    days: int = Query(30, ge=1, le=365, description="How many days back"),
):
    """Get OHLCV data for candlestick chart rendering.

    Returns data in lightweight-charts format:
    [{time, open, high, low, close, volume}, ...]
    Prices converted from cents to dollars for display.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with async_session() as session:
        stmt = (
            select(OHLCV)
            .where(
                OHLCV.symbol == symbol,
                OHLCV.timeframe == timeframe,
                OHLCV.timestamp >= cutoff,
            )
            .order_by(OHLCV.timestamp.asc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    candles = [
        {
            "time": int(r.timestamp.timestamp()),
            "open": r.open / 100,
            "high": r.high / 100,
            "low": r.low / 100,
            "close": r.close / 100,
            "volume": r.volume,
        }
        for r in rows
    ]

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "count": len(candles),
    }


@router.get("/chart/symbols")
async def get_available_symbols():
    """List all symbols with data in the database."""
    async with async_session() as session:
        stmt = (
            select(OHLCV.symbol, OHLCV.market, OHLCV.timeframe)
            .group_by(OHLCV.symbol, OHLCV.market, OHLCV.timeframe)
            .order_by(OHLCV.market, OHLCV.symbol)
        )
        result = await session.execute(stmt)
        combos = result.all()

    symbols = {}
    for symbol, market, timeframe in combos:
        if symbol not in symbols:
            symbols[symbol] = {"market": market, "timeframes": []}
        symbols[symbol]["timeframes"].append(timeframe)

    return {"symbols": symbols}
