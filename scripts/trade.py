"""CLI for trading operations.

Usage:
    python scripts/trade.py --paper    # Paper trading mode
    python scripts/trade.py --live     # LIVE trading (real money!)
    python scripts/trade.py --kill     # Emergency: close all positions
"""

import argparse
import asyncio
import logging
import signal
import sys

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = asyncio.Event()


def _handle_signal(sig: int, frame: object) -> None:
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    logger.info("Received signal %d, shutting down...", sig)
    _shutdown.set()


async def run_paper() -> None:
    """Run paper trading loop: evaluate signals every 5 min, check stops every 60s."""
    from app.services.execution.paper_executor import PaperExecutor
    from app.services.risk_manager import Order, RiskManager
    from app.services.strategy.auto_trader import AutoTrader, get_watched_symbols
    from app.services.data.market_calendar import Market, is_market_open

    risk_manager = RiskManager()
    executor = PaperExecutor(risk_manager)
    trader = AutoTrader()

    # Enable auto-trade on startup
    await trader.set_enabled(True)

    watched = await get_watched_symbols()
    print(f"Paper trading started. Evaluating {len(watched)} symbols every 5 minutes.")
    print("Press Ctrl+C to stop.\n")

    eval_interval = 300  # 5 minutes
    stop_check_interval = 60  # 1 minute
    last_eval = 0.0
    last_stop_check = 0.0

    try:
        while not _shutdown.is_set():
            now = asyncio.get_event_loop().time()

            # Evaluate signals every 5 minutes
            if now - last_eval >= eval_interval:
                last_eval = now
                logger.info("Evaluating signals...")

                open_positions = await executor.get_positions()
                held_symbols = {p["symbol"] for p in open_positions}
                signals_count = 0
                orders_count = 0

                for sym in watched:
                    try:
                        market_enum = Market(sym["market"])
                        if not is_market_open(market_enum):
                            continue

                        sig_result = await trader.evaluate_symbol(
                            sym["symbol"], sym["market"], sym["timeframe"]
                        )
                        if sig_result is None:
                            continue

                        # Skip sell signals for symbols we don't hold
                        if sig_result.action == "sell" and sig_result.symbol not in held_symbols:
                            continue

                        signals_count += 1
                        quantity_cents = sig_result.indicator_data.get("quantity_cents", 100)
                        order = Order(
                            symbol=sig_result.symbol,
                            market=sig_result.market,
                            side=sig_result.action,
                            order_type="market",
                            quantity_cents=quantity_cents,
                            price_cents=sig_result.price_cents,
                            stop_loss_cents=sig_result.stop_loss_cents,
                            strategy=sig_result.strategy_name,
                            reason=sig_result.reason,
                        )
                        result = await executor.submit_order(order)
                        if result.get("status") == "filled":
                            orders_count += 1
                            print(
                                f"  ORDER: {sig_result.action.upper()} {sig_result.symbol} "
                                f"@ ${sig_result.price_cents / 100:.2f} "
                                f"({sig_result.strategy_name}: {sig_result.reason})"
                            )
                    except Exception as e:
                        logger.error("Error evaluating %s: %s", sym["symbol"], e)

                print(
                    f"[eval] {signals_count} signals, {orders_count} orders, "
                    f"{len(held_symbols)} open positions"
                )

            # Check stop-losses every 60 seconds
            if now - last_stop_check >= stop_check_interval:
                last_stop_check = now
                try:
                    from app.services.data.ccxt_feed import CCXTFeed
                    from app.services.data.yfinance_feed import YFinanceFeed

                    positions = await executor.get_positions()
                    if positions:
                        prices: dict[str, int] = {}
                        try:
                            crypto_feed = CCXTFeed()
                            for p in await crypto_feed.get_prices():
                                prices[p.symbol] = p.price_cents
                        except Exception:
                            pass
                        try:
                            stock_feed = YFinanceFeed()
                            for p in await stock_feed.get_prices():
                                prices[p.symbol] = p.price_cents
                        except Exception:
                            pass

                        for pos in positions:
                            symbol = pos.get("symbol", "")
                            current_price = prices.get(symbol)
                            if current_price is None:
                                continue
                            stop_loss = pos.get("stop_loss_cents", 0)
                            if current_price <= stop_loss:
                                logger.warning("Stop-loss hit: %s @ %d", symbol, current_price)
                                await executor.close_position(symbol, current_price)
                                print(f"  STOP-LOSS: {symbol} closed @ ${current_price / 100:.2f}")
                except Exception as e:
                    logger.error("Stop-loss check error: %s", e)

            # Sleep 1 second between checks
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=1.0)
                break  # shutdown was set
            except asyncio.TimeoutError:
                pass

    finally:
        await trader.close()
        print("\nPaper trading stopped.")


async def run_kill() -> None:
    """Activate kill switch and close all positions."""
    from app.services.execution.paper_executor import PaperExecutor
    from app.services.risk_manager import RiskManager

    risk_manager = RiskManager()
    executor = PaperExecutor(risk_manager)

    # Activate kill switch
    risk_manager.kill_switch()
    print("Kill switch activated — all trading halted.")

    # Close all open positions
    results = await executor.close_all_positions()
    if results:
        for r in results:
            print(f"  Closed: {r['symbol']}")
        print(f"Closed {len(results)} position(s).")
    else:
        print("No open positions to close.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="FlashTrade trading")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--paper", action="store_true", help="Paper trading mode")
    group.add_argument("--live", action="store_true", help="LIVE trading (real money!)")
    group.add_argument("--kill", action="store_true", help="Emergency: close all positions")
    args = parser.parse_args()

    if args.kill:
        asyncio.run(run_kill())
    elif args.live:
        print("ERROR: Live trading disabled — no profitable strategy validated yet.")
        print("Run backtests with improved filters first, then update this guard.")
        sys.exit(1)
    elif args.paper:
        # Set up graceful shutdown
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        asyncio.run(run_paper())


if __name__ == "__main__":
    main()
