"""Dashboard API routes — portfolio overview, charts, positions."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/portfolio")
async def get_portfolio():
    """Get portfolio overview with positions and P&L."""
    return {
        "portfolio_value_cents": 1_000_000,  # $10,000 paper
        "daily_pnl_cents": 0,
        "positions": [],
        "status": "paper_trading",
    }


@router.get("/health")
async def health_check():
    """System health status."""
    return {"status": "ok", "trading_mode": "paper"}
