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
    from datetime import datetime, timezone

    import redis.asyncio as aioredis

    from app.api.admin import risk_manager
    from app.config import settings
    from app.services.execution.paper_executor import PaperExecutor
    from app.services.risk_manager import Order
    from app.services.strategy.auto_trader import AutoTrader, WATCHED_SYMBOLS

    trader = AutoTrader()

    # Record evaluation timestamp in Redis for the dashboard countdown timer
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.set(
            "flashtrade:last_evaluated_at",
            datetime.now(timezone.utc).isoformat(),
            ex=600,
        )
        await r.close()
    except Exception as e:
        logger.warning("Failed to record evaluation timestamp: %s", e)

    if not await trader.is_enabled():
        logger.info("Auto-trade disabled, skipping signal evaluation")
        return {"status": "disabled", "signals": 0}

    executor = PaperExecutor(risk_manager)
    signals_generated = 0
    orders_placed = 0
    results = []

    # Load open positions so we only sell what we actually hold
    open_positions = await executor.get_positions()
    held_symbols = {p["symbol"] for p in open_positions}

    for sym in WATCHED_SYMBOLS:
        try:
            signal = await trader.evaluate_symbol(
                sym["symbol"], sym["market"], sym["timeframe"]
            )
            if signal is None:
                results.append({"symbol": sym["symbol"], "action": "hold"})
                continue

            # Skip sell signals for symbols we don't hold
            if signal.action == "sell" and signal.symbol not in held_symbols:
                logger.info("Skipping sell signal for %s — no open position", signal.symbol)
                results.append({"symbol": sym["symbol"], "action": "sell_skipped", "reason": "no position"})
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
    from app.services.data.yfinance_feed import YFinanceFeed
    from app.services.execution.paper_executor import PaperExecutor

    executor = PaperExecutor(risk_manager)
    positions = await executor.get_positions()

    if not positions:
        return {"status": "ok", "checked": 0, "closed": 0}

    # Get current prices from all feeds (crypto + stocks)
    prices: dict[str, int] = {}
    errors = []

    # Crypto prices
    try:
        crypto_feed = CCXTFeed()
        crypto_raw = await crypto_feed.get_prices()
        for p in crypto_raw:
            prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.error("Failed to fetch crypto prices for stop-loss check: %s", e)
        errors.append(f"crypto: {e}")

    # Stock prices (ASX + US)
    try:
        stock_feed = YFinanceFeed()
        stock_raw = await stock_feed.get_prices()
        for p in stock_raw:
            prices[p.symbol] = p.price_cents
    except Exception as e:
        logger.error("Failed to fetch stock prices for stop-loss check: %s", e)
        errors.append(f"stocks: {e}")

    if not prices:
        return {"status": "error", "error": f"No price data available: {'; '.join(errors)}"}

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
