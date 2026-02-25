"""Admin API routes — kill switch, auto-trade control, system status, backtesting."""

import json
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.config import settings
from app.services.risk_manager import RiskManager
from app.services.strategy.auto_trader import (
    AutoTrader,
    DEFAULT_WATCHED_SYMBOLS,
    REDIS_KEY_WATCHED,
    get_watched_symbols,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

risk_manager = RiskManager()
_auto_trader = AutoTrader()


class AutoTradeRequest(BaseModel):
    """Request body for toggling auto-trade."""

    enabled: bool


@router.post("/kill-switch", dependencies=[Depends(require_api_key)])
async def activate_kill_switch():
    """Emergency: halt all trading and close all positions."""
    risk_manager.kill_switch()
    await _auto_trader.set_enabled(False)
    return {"status": "killed", "message": "All trading halted. Auto-trade disabled."}


@router.post("/resume", dependencies=[Depends(require_api_key)])
async def resume_trading():
    """Resume trading after kill switch (use with caution)."""
    risk_manager.reset_halt()
    return {"status": "resumed", "message": "Trading resumed. Monitor closely."}


@router.get("/status")
async def system_status():
    """Get current system status including auto-trade state."""
    try:
        auto_status = await _auto_trader.get_status()
    except Exception as e:
        logger.warning("Failed to get auto-trade status: %s", e)
        auto_status = {"enabled": False, "symbols": [], "error": "Redis/DB unavailable"}
    return {
        "halted": risk_manager.is_halted,
        "paused": risk_manager.is_paused,
        "trading_mode": "paper",
        "auto_trade": auto_status,
    }


@router.post("/auto-trade", dependencies=[Depends(require_api_key)])
async def toggle_auto_trade(req: AutoTradeRequest):
    """Enable or disable auto-trading."""
    await _auto_trader.set_enabled(req.enabled)
    return {
        "enabled": req.enabled,
        "message": f"Auto-trade {'enabled' if req.enabled else 'disabled'}",
    }


@router.get("/auto-trade")
async def get_auto_trade_status():
    """Get auto-trade status with regime info for all watched symbols."""
    return await _auto_trader.get_status()


@router.post("/backfill", dependencies=[Depends(require_api_key)])
async def trigger_backfill(period: str = "6mo"):
    """Trigger historical data backfill for all markets.

    Period: 1mo, 3mo, 6mo, 1y. Runs in-process (may take a few minutes).
    """
    from app.services.data.ingestion import backfill_all

    valid_periods = {"1mo", "3mo", "6mo", "1y"}
    if period not in valid_periods:
        return {"status": "error", "message": f"Invalid period. Use one of: {valid_periods}"}

    try:
        results = await backfill_all(period)
        errors = results.pop("_errors", [])
        total = (results.get("crypto_1h", 0) + results.get("crypto_4h", 0)
                 + results.get("crypto_1d", 0)
                 + results.get("asx_1d", 0) + results.get("asx_1h", 0)
                 + results.get("us_1d", 0) + results.get("us_1h", 0)
                 + results.get("uk_1d", 0) + results.get("uk_1h", 0))
        return {
            "status": "completed" if not errors else "partial",
            "period": period,
            "total_rows": total,
            "breakdown": results,
            "errors": errors,
        }
    except Exception as e:
        logger.error("Backfill failed: %s", e)
        return {"status": "error", "message": str(e)}


# ---------- Symbol management ----------


class AddSymbolRequest(BaseModel):
    """Request body for adding a watched symbol."""

    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = Field(..., pattern="^(crypto|asx|us|uk)$")
    timeframe: str = Field(default="1h", pattern="^(1h|4h|1d)$")


@router.get("/symbols")
async def list_symbols():
    """List all currently watched symbols."""
    symbols = await get_watched_symbols()
    return {
        "count": len(symbols),
        "symbols": symbols,
    }


@router.post("/symbols", dependencies=[Depends(require_api_key)])
async def add_symbol(req: AddSymbolRequest):
    """Add a symbol to the watchlist."""
    # Validate ASX suffix
    if req.market == "asx" and not req.symbol.endswith(".AX"):
        return {"status": "error", "message": "ASX symbols must end with .AX (e.g., BHP.AX)"}
    if req.market == "uk" and not req.symbol.endswith(".L"):
        return {"status": "error", "message": "UK symbols must end with .L (e.g., SHEL.L)"}

    symbols = await get_watched_symbols()

    # Check for duplicate
    for s in symbols:
        if s["symbol"].upper() == req.symbol.upper():
            return {"status": "error", "message": f"{req.symbol} is already in the watchlist"}

    symbols.append({
        "symbol": req.symbol.upper(),
        "market": req.market,
        "timeframe": req.timeframe,
    })

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set(REDIS_KEY_WATCHED, json.dumps(symbols))
    await r.aclose()

    return {"status": "added", "symbol": req.symbol.upper(), "count": len(symbols)}


@router.delete("/symbols/{symbol}", dependencies=[Depends(require_api_key)])
async def remove_symbol(symbol: str):
    """Remove a symbol from the watchlist."""
    symbols = await get_watched_symbols()
    original_len = len(symbols)

    symbols = [s for s in symbols if s["symbol"].upper() != symbol.upper()]

    if len(symbols) == original_len:
        return {"status": "error", "message": f"{symbol} not found in watchlist"}

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set(REDIS_KEY_WATCHED, json.dumps(symbols))
    await r.aclose()

    return {"status": "removed", "symbol": symbol.upper(), "count": len(symbols)}


@router.post("/symbols/reset", dependencies=[Depends(require_api_key)])
async def reset_symbols():
    """Reset watchlist to default 30 symbols."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.delete(REDIS_KEY_WATCHED)
    await r.aclose()

    return {
        "status": "reset",
        "count": len(DEFAULT_WATCHED_SYMBOLS),
        "message": "Watchlist reset to defaults",
    }


class BacktestRequest(BaseModel):
    """Request body for backtesting via API."""

    strategy: str = Field(..., pattern="^(momentum|meanrev|turtle_crypto|turtle_stocks|auto)$")
    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = Field(..., pattern="^(crypto|us|asx|uk)$")
    timeframe: str = Field(default="1h", pattern="^(1h|4h|1d)$")
    days: int = Field(default=180, ge=30, le=730)


@router.post("/backtest", dependencies=[Depends(require_api_key)])
async def run_backtest(req: BacktestRequest):
    """Run a backtest and return results as JSON.

    This can take 10-60 seconds depending on data size and timeframe.
    """
    from app.services.backtest.engine import BacktestEngine

    try:
        engine = BacktestEngine(
            strategy_name=req.strategy,
            symbol=req.symbol,
            market=req.market,
            timeframe=req.timeframe,
            days=req.days,
            auto_regime=(req.strategy == "auto"),
        )
        result = await engine.run()
        return {"status": "completed", "result": result.to_dict()}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error("Backtest failed for %s: %s", req.symbol, e)
        return {"status": "error", "message": f"Backtest failed: {e}"}
