"""Dashboard API routes — portfolio overview, live prices, health, data quality, charts."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis

from app.config import settings

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.data.feeds import ccxt_feed as _ccxt_feed
from app.services.data.feeds import get_live_prices as _fetch_live_prices
from app.services.data.feeds import yfinance_feed as _yfinance_feed
from app.services.data.market_calendar import market_status_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Portfolio cache — avoids hitting DB + 2 external APIs every 15s poll
_portfolio_cache: dict | None = None
_portfolio_cache_time: float = 0.0
_PORTFOLIO_CACHE_TTL = 15  # seconds


@router.get("/portfolio")
async def get_portfolio():
    """Get portfolio overview with positions, cash, and P&L.

    Starting cash: $10,000 AUD (1,000,000 cents).
    Cash decreases when buying, increases when selling.
    Unrealized P&L calculated from live prices, not stale DB values.
    Cached for 15 seconds to avoid hammering external APIs on every poll.
    """
    global _portfolio_cache, _portfolio_cache_time

    now = time.monotonic()
    if _portfolio_cache is not None and (now - _portfolio_cache_time) < _PORTFOLIO_CACHE_TTL:
        return _portfolio_cache

    from app.services.execution.paper_executor import PaperExecutor
    from app.api.admin import risk_manager

    STARTING_CASH_CENTS = 1_000_000  # $10,000 AUD

    executor = PaperExecutor(risk_manager)
    positions = await executor.get_positions()

    # Fetch live prices to calculate real unrealized P&L (uses shared cached singletons)
    live_prices = await _fetch_live_prices()

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

    result = {
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
    _portfolio_cache = result
    _portfolio_cache_time = now
    return result


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


REDIS_KEY_INDICES = "flashtrade:indices"
REDIS_KEY_TURTLE_SCAN = "flashtrade:turtle_scan"


@router.get("/indices")
async def get_market_indices():
    """Get major market index levels (ASX 200, FTSE 100, NASDAQ, S&P 500).

    Cached in Redis for 5 minutes.
    """
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        cached = await r.get(REDIS_KEY_INDICES)
        if cached:
            await r.aclose()
            return {"indices": json.loads(cached)}

        from app.services.data.yfinance_feed import get_indices
        indices = await get_indices()
        if indices:
            await r.set(REDIS_KEY_INDICES, json.dumps(indices), ex=300)
        await r.aclose()
        return {"indices": indices}
    except Exception as e:
        logger.error("Failed to fetch indices: %s", e)
        return {"indices": [], "error": str(e)}


@router.get("/turtle-scan")
async def get_turtle_scan():
    """Scan all watched symbols for turtle breakout proximity.

    Returns Donchian channel levels, ATR, breakout distance %, system type,
    and whether each symbol is near a breakout. Cached for 5 minutes.
    """
    import pandas as pd
    from app.services.strategy.auto_trader import get_watched_symbols
    from app.services.strategy.indicators import atr as calc_atr, donchian_channel

    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        cached = await r.get(REDIS_KEY_TURTLE_SCAN)
        if cached:
            await r.aclose()
            return json.loads(cached)

        watched = await get_watched_symbols(redis_conn=r)

        results = []
        async with async_session() as session:
            for sym in watched:
                symbol = sym["symbol"]
                market = sym["market"]
                timeframe = sym["timeframe"]

                is_crypto = market == "crypto"
                entry_period = 15 if is_crypto else 20
                long_entry_period = 40 if is_crypto else 55
                exit_period = 8 if is_crypto else 10
                stop_mult = 2.5 if is_crypto else 2.0

                cutoff = datetime.now(timezone.utc) - timedelta(days=90)
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

                min_bars = max(entry_period, long_entry_period) + 5
                if len(rows) < min_bars:
                    continue

                df = pd.DataFrame({
                    "timestamp": [row.timestamp for row in rows],
                    "open": [float(row.open) for row in rows],
                    "high": [float(row.high) for row in rows],
                    "low": [float(row.low) for row in rows],
                    "close": [float(row.close) for row in rows],
                    "volume": [float(row.volume) for row in rows],
                })
                df.set_index("timestamp", inplace=True)

                close = df["close"]
                high = df["high"]
                low = df["low"]

                # Donchian channels (shifted to avoid look-ahead)
                dc_upper, dc_lower, _ = donchian_channel(
                    high.shift(1), low.shift(1), entry_period
                )
                long_upper, _, _ = donchian_channel(
                    high.shift(1), low.shift(1), long_entry_period
                )
                _, exit_lower, _ = donchian_channel(
                    high.shift(1), low.shift(1), exit_period
                )
                atr_values = calc_atr(high, low, close, period=20)

                current_close = float(close.iloc[-1])
                cur_dc_upper = float(dc_upper.iloc[-1]) if not pd.isna(dc_upper.iloc[-1]) else None
                cur_long_upper = float(long_upper.iloc[-1]) if not pd.isna(long_upper.iloc[-1]) else None
                cur_exit_lower = float(exit_lower.iloc[-1]) if not pd.isna(exit_lower.iloc[-1]) else None
                cur_atr = float(atr_values.iloc[-1]) if not pd.isna(atr_values.iloc[-1]) else None

                if cur_dc_upper is None:
                    continue

                # Breakout distance %
                breakout_dist_pct = round(
                    (current_close - cur_dc_upper) / cur_dc_upper * 100, 2
                ) if cur_dc_upper > 0 else None

                # System classification
                above_short = current_close > cur_dc_upper
                above_long = cur_long_upper is not None and current_close > cur_long_upper

                if above_long:
                    system = "System 2 (breakout)"
                elif above_short:
                    system = "System 1 (breakout)"
                elif breakout_dist_pct is not None and breakout_dist_pct > -3:
                    system = "Approaching"
                else:
                    system = "Waiting"

                # Stop level if entered
                stop_level = round(
                    current_close - stop_mult * cur_atr, 2
                ) if cur_atr else None

                results.append({
                    "symbol": symbol,
                    "market": market,
                    "price": round(current_close, 2),
                    "donchian_upper": round(cur_dc_upper, 2),
                    "donchian_long_upper": round(cur_long_upper, 2) if cur_long_upper else None,
                    "donchian_exit_lower": round(cur_exit_lower, 2) if cur_exit_lower else None,
                    "atr_20": round(cur_atr, 2) if cur_atr else None,
                    "breakout_dist_pct": breakout_dist_pct,
                    "system": system,
                    "stop_level": stop_level,
                    "entry_period": entry_period,
                    "long_entry_period": long_entry_period,
                    "exit_period": exit_period,
                    "stop_multiplier": stop_mult,
                })

        # Sort: breakouts first, then closest to breakout
        results.sort(key=lambda x: -(x["breakout_dist_pct"] or -999))

        payload = {
            "symbols": results,
            "count": len(results),
            "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        await r.set(REDIS_KEY_TURTLE_SCAN, json.dumps(payload), ex=300)
        await r.aclose()
        return payload

    except Exception as e:
        logger.error("Turtle scan failed: %s", e)
        return {"symbols": [], "count": 0, "error": str(e)}
