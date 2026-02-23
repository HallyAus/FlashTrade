"""Admin API routes — kill switch, auto-trade control, system status."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.risk_manager import RiskManager
from app.services.strategy.auto_trader import AutoTrader

router = APIRouter(prefix="/api/admin", tags=["admin"])

risk_manager = RiskManager()
_auto_trader = AutoTrader()


class AutoTradeRequest(BaseModel):
    """Request body for toggling auto-trade."""

    enabled: bool


@router.post("/kill-switch")
async def activate_kill_switch():
    """Emergency: halt all trading and close all positions."""
    risk_manager.kill_switch()
    await _auto_trader.set_enabled(False)
    return {"status": "killed", "message": "All trading halted. Auto-trade disabled."}


@router.post("/resume")
async def resume_trading():
    """Resume trading after kill switch (use with caution)."""
    risk_manager.reset_halt()
    return {"status": "resumed", "message": "Trading resumed. Monitor closely."}


@router.get("/status")
async def system_status():
    """Get current system status including auto-trade state."""
    try:
        auto_status = await _auto_trader.get_status()
    except Exception:
        auto_status = {"enabled": False, "symbols": [], "error": "Redis/DB unavailable"}
    return {
        "halted": risk_manager.is_halted,
        "paused": risk_manager.is_paused,
        "trading_mode": "paper",
        "auto_trade": auto_status,
    }


@router.post("/auto-trade")
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
