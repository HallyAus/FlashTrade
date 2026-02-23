"""Trade API routes — place trades, view history, manage positions."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.admin import risk_manager
from app.api.auth import require_api_key
from app.config import settings
from app.services.execution.paper_executor import PaperExecutor
from app.services.risk_manager import Order

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trades", tags=["trades"])

# Module-level executor singleton
_paper_executor = PaperExecutor(risk_manager)


class TradeRequest(BaseModel):
    """Request body for placing a trade."""

    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Z0-9.]+$")
    market: Literal["crypto", "asx", "us"] = "crypto"
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop"] = "market"
    quantity_cents: int = Field(..., ge=100, le=10000, description="Position size in cents (AUD), min $1, max $100")
    price_cents: int = Field(..., gt=0, description="Current price in cents, must be positive")
    stop_loss_cents: int = Field(..., gt=0, description="Stop-loss price in cents, must be positive")
    strategy: str = Field("manual", max_length=50)
    reason: str = Field("Manual trade via dashboard", max_length=200)


@router.post("/", dependencies=[Depends(require_api_key)])
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
    """List open positions with live prices and unrealized P&L."""
    from app.services.data.ccxt_feed import CCXTFeed
    from app.services.data.yfinance_feed import YFinanceFeed

    positions = await _paper_executor.get_positions()

    # Enrich with live prices
    live_prices = {}
    try:
        ccxt_feed = CCXTFeed()
        for p in await ccxt_feed.get_prices():
            live_prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.warning("Failed to fetch crypto prices for positions: %s", e)
    try:
        yf_feed = YFinanceFeed()
        for p in await yf_feed.get_prices():
            live_prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.warning("Failed to fetch stock prices for positions: %s", e)

    for pos in positions:
        live_price = live_prices.get(pos["symbol"])
        if live_price:
            pos["current_price_cents"] = live_price
            entry = pos["entry_price_cents"]
            if entry > 0:
                pos["unrealized_pnl_cents"] = int(
                    pos["quantity"] * (live_price - entry) / entry
                )

    return {"positions": positions}
