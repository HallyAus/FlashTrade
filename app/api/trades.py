"""Trade API routes — place trades, view history, manage positions."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.admin import risk_manager
from app.services.execution.paper_executor import PaperExecutor
from app.services.risk_manager import Order

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trades", tags=["trades"])

# Module-level executor singleton
_paper_executor = PaperExecutor(risk_manager)


class TradeRequest(BaseModel):
    """Request body for placing a trade."""

    symbol: str
    market: str = "crypto"
    side: str  # buy, sell
    order_type: str = "market"
    quantity_cents: int  # position size in cents (AUD)
    price_cents: int  # current price in cents
    stop_loss_cents: int  # mandatory
    strategy: str = "manual"
    reason: str = "Manual trade via dashboard"


@router.post("/")
async def place_trade(req: TradeRequest):
    """Place a new trade through risk manager and paper executor."""
    order = Order(
        symbol=req.symbol,
        market=req.market,
        side=req.side,
        order_type=req.order_type,
        quantity_cents=req.quantity_cents,
        price_cents=req.price_cents,
        stop_loss_cents=req.stop_loss_cents,
        strategy=req.strategy,
        reason=req.reason,
    )

    result = await _paper_executor.submit_order(order)
    return result


@router.get("/")
async def list_trades():
    """List recent trades."""
    trades = await _paper_executor.get_trades(limit=50)
    return {"trades": trades}


@router.get("/positions")
async def list_positions():
    """List open positions."""
    positions = await _paper_executor.get_positions()
    return {"positions": positions}
