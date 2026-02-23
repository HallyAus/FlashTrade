"""Paper trading executor — simulates order execution without real money.

Records all trades and positions in the database exactly like a real executor,
so strategies can be validated before risking capital.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.position import Position
from app.models.trade import Trade
from app.models.journal import JournalEntry
from app.services.risk_manager import Order, RiskManager, RiskVerdict

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Simulate order execution for paper trading.

    - All trades go through RiskManager before execution
    - Fills instantly at the requested price (no slippage simulation yet)
    - Persists trades, positions, and journal entries to the database
    """

    def __init__(self, risk_manager: RiskManager) -> None:
        self._risk_manager = risk_manager

    async def submit_order(self, order: Order) -> dict:
        """Submit an order through risk checks and execute if approved.

        Returns a dict with status, trade_id, and details.
        """
        # Step 1: Risk check
        verdict: RiskVerdict = self._risk_manager.evaluate(order)
        if not verdict.approved:
            logger.warning("Order rejected: %s", verdict.reason)
            # Record rejected trade
            async with async_session() as session:
                trade = Trade(
                    symbol=order.symbol,
                    market=order.market,
                    side=order.side,
                    order_type=order.order_type,
                    quantity_cents=order.quantity_cents,
                    price_cents=order.price_cents,
                    stop_loss_cents=order.stop_loss_cents,
                    status="rejected",
                    strategy=order.strategy,
                    reason=verdict.reason,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(trade)
                await session.commit()
                await session.refresh(trade)

                return {
                    "status": "rejected",
                    "trade_id": trade.id,
                    "reason": verdict.reason,
                }

        # Step 2: For sells, verify we actually hold the position first
        if order.side == "sell":
            async with async_session() as session:
                existing = await session.execute(
                    select(Position).where(Position.symbol == order.symbol)
                )
                if existing.scalar_one_or_none() is None:
                    logger.warning("Sell rejected: no open position for %s", order.symbol)
                    trade = Trade(
                        symbol=order.symbol,
                        market=order.market,
                        side=order.side,
                        order_type=order.order_type,
                        quantity_cents=order.quantity_cents,
                        price_cents=order.price_cents,
                        stop_loss_cents=order.stop_loss_cents,
                        status="rejected",
                        strategy=order.strategy,
                        reason="No open position to sell",
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(trade)
                    await session.commit()
                    await session.refresh(trade)
                    return {
                        "status": "rejected",
                        "trade_id": trade.id,
                        "reason": "No open position to sell",
                    }

        # Step 3: Execute (instant fill for paper trading)
        async with async_session() as session:
            now = datetime.now(timezone.utc)

            # Create trade record
            trade = Trade(
                symbol=order.symbol,
                market=order.market,
                side=order.side,
                order_type=order.order_type,
                quantity_cents=order.quantity_cents,
                price_cents=order.price_cents,
                stop_loss_cents=order.stop_loss_cents,
                status="filled",
                strategy=order.strategy,
                broker_order_id=f"paper-{now.strftime('%Y%m%d%H%M%S')}",
                reason=order.reason,
                created_at=now,
                filled_at=now,
            )
            session.add(trade)
            await session.flush()

            # Step 4: Update positions
            if order.side == "buy":
                await self._open_or_add_position(session, order, trade.id, now)
            elif order.side == "sell":
                pnl = await self._close_position(session, order, now)
                if pnl is not None:
                    self._risk_manager.record_trade_result(pnl)

            # Step 4: Journal entry
            journal = JournalEntry(
                trade_id=trade.id,
                symbol=order.symbol,
                action=f"paper_{order.side}",
                strategy=order.strategy,
                reasoning=order.reason,
                created_at=now,
            )
            session.add(journal)
            await session.commit()

            logger.info(
                "Paper trade executed: %s %s %d cents @ %d cents",
                order.side, order.symbol, order.quantity_cents, order.price_cents,
            )
            return {
                "status": "filled",
                "trade_id": trade.id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity_cents": order.quantity_cents,
                "price_cents": order.price_cents,
            }

    async def _open_or_add_position(
        self, session: AsyncSession, order: Order, trade_id: int, now: datetime
    ) -> None:
        """Open a new position or add to an existing one.

        Uses SELECT FOR UPDATE to prevent race conditions from concurrent workers.
        """
        existing = await session.execute(
            select(Position)
            .where(Position.symbol == order.symbol)
            .with_for_update()
        )
        pos = existing.scalar_one_or_none()

        if pos:
            # Average into existing position (weighted average entry price)
            old_cost = pos.entry_price_cents * pos.quantity
            new_cost = order.price_cents * order.quantity_cents
            new_total_qty = pos.quantity + order.quantity_cents
            pos.entry_price_cents = int((old_cost + new_cost) / new_total_qty) if new_total_qty > 0 else order.price_cents
            pos.quantity = new_total_qty
            pos.current_price_cents = order.price_cents
            pos.stop_loss_cents = order.stop_loss_cents
            pos.updated_at = now
        else:
            # New position
            pos = Position(
                symbol=order.symbol,
                market=order.market,
                side="long",
                quantity=order.quantity_cents,
                entry_price_cents=order.price_cents,
                current_price_cents=order.price_cents,
                stop_loss_cents=order.stop_loss_cents,
                strategy=order.strategy,
                unrealized_pnl_cents=0,
                opened_at=now,
                updated_at=now,
            )
            session.add(pos)

    async def _close_position(
        self, session: AsyncSession, order: Order, now: datetime
    ) -> int | None:
        """Close a position and calculate realized P&L. Returns pnl_cents or None."""
        existing = await session.execute(
            select(Position)
            .where(Position.symbol == order.symbol)
            .with_for_update()
        )
        pos = existing.scalar_one_or_none()

        if not pos:
            logger.warning("No position to close for %s", order.symbol)
            return None

        # Calculate P&L: quantity * (sell_price - entry_price) / entry_price
        if pos.entry_price_cents > 0:
            pnl_cents = int(pos.quantity * (order.price_cents - pos.entry_price_cents) / pos.entry_price_cents)
        else:
            pnl_cents = 0

        # Remove position
        await session.execute(
            delete(Position).where(Position.symbol == order.symbol)
        )

        logger.info(
            "Position closed: %s, P&L: %d cents ($%.2f)",
            order.symbol, pnl_cents, pnl_cents / 100,
        )
        return pnl_cents

    async def get_positions(self) -> list[dict]:
        """Get all open positions."""
        async with async_session() as session:
            result = await session.execute(select(Position))
            positions = result.scalars().all()
            return [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "market": p.market,
                    "side": p.side,
                    "quantity": p.quantity,
                    "entry_price_cents": p.entry_price_cents,
                    "current_price_cents": p.current_price_cents,
                    "stop_loss_cents": p.stop_loss_cents,
                    "strategy": p.strategy,
                    "unrealized_pnl_cents": p.unrealized_pnl_cents,
                    "opened_at": p.opened_at.isoformat(),
                }
                for p in positions
            ]

    async def get_trades(self, limit: int = 50) -> list[dict]:
        """Get recent trades."""
        async with async_session() as session:
            result = await session.execute(
                select(Trade).order_by(Trade.created_at.desc()).limit(limit)
            )
            trades = result.scalars().all()
            return [
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "market": t.market,
                    "side": t.side,
                    "order_type": t.order_type,
                    "quantity_cents": t.quantity_cents,
                    "price_cents": t.price_cents,
                    "stop_loss_cents": t.stop_loss_cents,
                    "status": t.status,
                    "strategy": t.strategy,
                    "reason": t.reason,
                    "created_at": t.created_at.isoformat(),
                    "filled_at": t.filled_at.isoformat() if t.filled_at else None,
                }
                for t in trades
            ]

    async def close_position(self, symbol: str, current_price_cents: int) -> dict:
        """Close a specific position at the given price."""
        order = Order(
            symbol=symbol,
            market="crypto",
            side="sell",
            order_type="market",
            quantity_cents=1,  # Will be overridden by actual position size
            price_cents=current_price_cents,
            stop_loss_cents=current_price_cents,
            strategy="stop_loss",
            reason=f"Stop-loss triggered at {current_price_cents} cents",
        )
        async with async_session() as session:
            now = datetime.now(timezone.utc)

            # Get the actual position
            result = await session.execute(
                select(Position).where(Position.symbol == symbol)
            )
            pos = result.scalar_one_or_none()
            if not pos:
                return {"status": "no_position", "symbol": symbol}

            # Calculate P&L: quantity * (sell_price - entry_price) / entry_price
            if pos.entry_price_cents > 0:
                pnl_cents = int(pos.quantity * (current_price_cents - pos.entry_price_cents) / pos.entry_price_cents)
            else:
                pnl_cents = 0
            self._risk_manager.record_trade_result(pnl_cents)

            trade = Trade(
                symbol=symbol,
                market=pos.market,
                side="sell",
                order_type="market",
                quantity_cents=pos.quantity,
                price_cents=current_price_cents,
                stop_loss_cents=pos.stop_loss_cents,
                status="filled",
                strategy="stop_loss",
                reason=f"Stop-loss triggered. P&L: {pnl_cents} cents",
                created_at=now,
                filled_at=now,
            )
            session.add(trade)
            await session.flush()

            journal = JournalEntry(
                trade_id=trade.id,
                symbol=symbol,
                action="stop_loss_close",
                strategy=pos.strategy,
                reasoning=f"Stop-loss hit at {current_price_cents} cents. P&L: {pnl_cents} cents",
                created_at=now,
            )
            session.add(journal)

            await session.execute(
                delete(Position).where(Position.symbol == symbol)
            )
            await session.commit()

            logger.info("Stop-loss close: %s, P&L: %d cents", symbol, pnl_cents)
            return {"status": "closed", "symbol": symbol, "pnl_cents": pnl_cents}

    async def close_all_positions(self) -> list[dict]:
        """Emergency: close all open positions at current price."""
        positions = await self.get_positions()
        results = []
        for pos in positions:
            order = Order(
                symbol=pos["symbol"],
                market=pos["market"],
                side="sell",
                order_type="market",
                quantity_cents=pos["quantity"],
                price_cents=pos["current_price_cents"],
                stop_loss_cents=0,
                strategy="kill_switch",
                reason="Emergency close via kill switch",
            )
            # Bypass risk manager for emergency close
            async with async_session() as session:
                now = datetime.now(timezone.utc)
                trade = Trade(
                    symbol=order.symbol,
                    market=order.market,
                    side="sell",
                    order_type="market",
                    quantity_cents=order.quantity_cents,
                    price_cents=order.price_cents,
                    stop_loss_cents=0,
                    status="filled",
                    strategy="kill_switch",
                    reason="Emergency close",
                    created_at=now,
                    filled_at=now,
                )
                session.add(trade)
                await session.execute(
                    delete(Position).where(Position.symbol == order.symbol)
                )
                await session.commit()
                results.append({"symbol": order.symbol, "closed": True})

        return results
