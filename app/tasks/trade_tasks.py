"""Trading execution tasks — strategy evaluation and order placement.

Celery tasks that run on a schedule to evaluate signals and manage positions.
All trades go through RiskManager before execution.
"""

import asyncio
import logging

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=1)
def evaluate_signals(self) -> dict:
    """Run strategies against latest data and generate signals.

    For each watched symbol:
    1. Check if auto-trade is enabled
    2. Detect market regime
    3. Pick strategy (momentum for trending, meanrev for ranging)
    4. Generate signal
    5. Submit order through paper executor if signal found
    """
    return _run_async(_evaluate_signals_async())


async def _evaluate_signals_async() -> dict:
    from app.api.admin import risk_manager
    from app.services.execution.paper_executor import PaperExecutor
    from app.services.risk_manager import Order
    from app.services.strategy.auto_trader import AutoTrader, WATCHED_SYMBOLS

    trader = AutoTrader()

    if not await trader.is_enabled():
        logger.info("Auto-trade disabled, skipping signal evaluation")
        return {"status": "disabled", "signals": 0}

    executor = PaperExecutor(risk_manager)
    signals_generated = 0
    orders_placed = 0
    results = []

    for sym in WATCHED_SYMBOLS:
        try:
            signal = await trader.evaluate_symbol(
                sym["symbol"], sym["market"], sym["timeframe"]
            )
            if signal is None:
                results.append({"symbol": sym["symbol"], "action": "hold"})
                continue

            signals_generated += 1

            # Convert signal to order
            quantity_cents = signal.indicator_data.get("quantity_cents", 100)
            order = Order(
                symbol=signal.symbol,
                market=signal.market,
                side=signal.action,
                order_type="market",
                quantity_cents=quantity_cents,
                price_cents=signal.price_cents,
                stop_loss_cents=signal.stop_loss_cents,
                strategy=signal.strategy_name,
                reason=signal.reason,
            )

            result = await executor.submit_order(order)
            if result.get("status") == "filled":
                orders_placed += 1
            results.append({
                "symbol": sym["symbol"],
                "action": signal.action,
                "status": result.get("status"),
                "reason": result.get("reason", ""),
            })

        except Exception as e:
            logger.error("Error evaluating %s: %s", sym["symbol"], e)
            results.append({"symbol": sym["symbol"], "error": str(e)})

    logger.info(
        "Signal evaluation complete: %d signals, %d orders placed",
        signals_generated, orders_placed,
    )
    return {
        "status": "completed",
        "signals": signals_generated,
        "orders": orders_placed,
        "results": results,
    }


@celery_app.task(bind=True, max_retries=1)
def check_stop_losses(self) -> dict:
    """Check open positions against current prices and close if stop-loss hit."""
    return _run_async(_check_stop_losses_async())


async def _check_stop_losses_async() -> dict:
    from app.api.admin import risk_manager
    from app.services.data.ccxt_feed import CCXTFeed
    from app.services.execution.paper_executor import PaperExecutor

    executor = PaperExecutor(risk_manager)
    positions = await executor.get_positions()

    if not positions:
        return {"status": "ok", "checked": 0, "closed": 0}

    # Get current prices
    feed = CCXTFeed()
    try:
        prices_raw = await feed.get_prices()
        prices = {p.symbol: p.price_cents for p in prices_raw}
    except Exception as e:
        logger.error("Failed to fetch prices for stop-loss check: %s", e)
        return {"status": "error", "error": str(e)}

    closed = 0
    for pos in positions:
        symbol = pos.get("symbol", "")
        current_price = prices.get(symbol)
        if current_price is None:
            continue

        stop_loss = pos.get("stop_loss_cents", 0)
        side = pos.get("side", "long")

        # Check if stop-loss hit
        hit = False
        if side == "long" and current_price <= stop_loss:
            hit = True
        elif side == "short" and current_price >= stop_loss:
            hit = True

        if hit:
            logger.warning(
                "Stop-loss hit for %s: price=%d, stop=%d",
                symbol, current_price, stop_loss,
            )
            try:
                await executor.close_position(symbol, current_price)
                closed += 1
            except Exception as e:
                logger.error("Failed to close position %s: %s", symbol, e)

    return {"status": "ok", "checked": len(positions), "closed": closed}
