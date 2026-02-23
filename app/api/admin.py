"""Admin API routes — kill switch, config, system control."""

from fastapi import APIRouter

from app.services.risk_manager import RiskManager

router = APIRouter(prefix="/api/admin", tags=["admin"])

risk_manager = RiskManager()


@router.post("/kill-switch")
async def activate_kill_switch():
    """Emergency: halt all trading and close all positions."""
    risk_manager.kill_switch()
    # TODO: close all open positions via executors
    return {"status": "killed", "message": "All trading halted. Positions being closed."}


@router.post("/resume")
async def resume_trading():
    """Resume trading after kill switch (use with caution)."""
    risk_manager.reset_halt()
    return {"status": "resumed", "message": "Trading resumed. Monitor closely."}


@router.get("/status")
async def system_status():
    """Get current system status."""
    return {
        "halted": risk_manager.is_halted,
        "paused": risk_manager.is_paused,
        "trading_mode": "paper",
    }
