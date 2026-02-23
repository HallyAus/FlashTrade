"""Trade API routes — trade history, active orders."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get("/")
async def list_trades():
    """List recent trades."""
    return {"trades": []}


@router.get("/positions")
async def list_positions():
    """List open positions."""
    return {"positions": []}
