"""Simulated broker for backtesting. Handles fills, fees, stop-losses.

Matches PaperExecutor logic: one position per symbol, same P&L formula,
same position sizing. Adds fee/spread simulation for realistic results.
"""

import logging
from datetime import datetime

from app.services.backtest.result import ClosedTrade
from app.services.strategy.base import Signal

logger = logging.getLogger(__name__)

# One-way fee rates per market (applied on both entry and exit)
FEE_RATES: dict[str, float] = {
    "crypto": 0.004,  # 0.4% Swyftx spread (taker)
    "crypto_maker": 0.001,  # 0.1% maker fee (limit orders)
    "asx": 0.001,  # 0.1% brokerage estimate
    "us": 0.001,  # 0.1% Alpaca spread estimate
}


class _Position:
    """Internal position tracker during backtest."""

    __slots__ = (
        "symbol", "market", "entry_price_cents", "quantity_cents",
        "stop_loss_cents", "strategy", "entry_time", "entry_bar_index",
        "pyramid_count",
    )

    def __init__(
        self,
        symbol: str,
        market: str,
        entry_price_cents: int,
        quantity_cents: int,
        stop_loss_cents: int,
        strategy: str,
        entry_time: datetime,
        entry_bar_index: int,
    ) -> None:
        self.symbol = symbol
        self.market = market
        self.entry_price_cents = entry_price_cents
        self.quantity_cents = quantity_cents
        self.stop_loss_cents = stop_loss_cents
        self.strategy = strategy
        self.entry_time = entry_time
        self.entry_bar_index = entry_bar_index
        self.pyramid_count: int = 1


class BacktestBroker:
    """Simulates order execution with position management and fee modeling.

    Rules (matching PaperExecutor):
    - One position per symbol at a time
    - Buy signals open a position (if none exists)
    - Sell signals close a position (if one exists)
    - Stop-losses checked against each bar's low price (intrabar sim)
    - Position sizing capped at max_position_size_cents
    - Fees applied as spread on entry and exit
    """

    def __init__(
        self,
        starting_cash_cents: int = 1_000_000,
        max_position_size_cents: int = 10_000,
        cooldown_bars: int = 0,
        fee_tier: str = "default",
    ) -> None:
        self.cash_cents: int = starting_cash_cents
        self.starting_cash_cents: int = starting_cash_cents
        self.max_position_size_cents = max_position_size_cents
        self._cooldown_bars = cooldown_bars
        self._fee_tier = fee_tier
        self._last_close_bar_index: int = -999
        self._position: _Position | None = None
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[dict] = []
        self.total_fees_cents: int = 0

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def position(self) -> _Position | None:
        return self._position

    def process_bar(
        self,
        signal: Signal | None,
        bar_high_cents: int,
        bar_low_cents: int,
        bar_close_cents: int,
        bar_time: datetime,
        bar_index: int,
        market: str,
    ) -> None:
        """Process one bar: check stop-loss, then handle signal, then record equity.

        Order of operations:
        1. Check if stop-loss was hit (using bar_low for longs)
        2. If signal is "sell" and we have a position, close it
        3. If signal is "buy" and no position, open one
        4. Record equity snapshot
        """
        # 1. Stop-loss check (before signal processing)
        if self._position is not None:
            if bar_low_cents <= self._position.stop_loss_cents:
                self._close_position(
                    price_cents=self._position.stop_loss_cents,
                    bar_time=bar_time,
                    bar_index=bar_index,
                    reason="stop_loss",
                    market=market,
                )

        # 2. Process signal
        if signal is not None and signal.action == "sell" and self._position is not None:
            self._close_position(
                price_cents=int(signal.price_cents),
                bar_time=bar_time,
                bar_index=bar_index,
                reason="signal",
                market=market,
            )
        elif signal is not None and signal.action == "buy" and self._position is None:
            self._open_position(signal, bar_time, bar_index, market)
        elif signal is not None and signal.action == "buy" and self._position is not None:
            # Pyramiding: add to existing position (e.g. Turtle Trading)
            self._add_to_position(signal, bar_time, bar_index, market)

        # 3. Record equity
        self._record_equity(bar_close_cents, bar_time)

    def _get_fee_rate(self, market: str) -> float:
        """Get the fee rate based on market and fee tier."""
        if self._fee_tier == "maker" and market == "crypto":
            return FEE_RATES.get("crypto_maker", 0.001)
        return FEE_RATES.get(market, 0.001)

    def _open_position(
        self, signal: Signal, bar_time: datetime, bar_index: int, market: str
    ) -> None:
        """Open a new position from a buy signal."""
        # Cooldown check: skip entry if too soon after last close
        if self._cooldown_bars > 0:
            bars_since_close = bar_index - self._last_close_bar_index
            if bars_since_close < self._cooldown_bars:
                logger.debug(
                    "Cooldown active: %d bars since last close (need %d)",
                    bars_since_close, self._cooldown_bars,
                )
                return

        # Apply entry fee (buying at slightly higher price due to spread)
        fee_rate = self._get_fee_rate(market)
        fill_price = int(signal.price_cents * (1 + fee_rate))

        # Position size from signal (already sized by engine)
        quantity_cents = signal.indicator_data.get("quantity_cents", 100)
        quantity_cents = min(quantity_cents, self.max_position_size_cents)
        quantity_cents = min(quantity_cents, self.cash_cents)  # Can't spend more than we have

        if quantity_cents < 100:  # Minimum $1 position
            return

        # Fee is proportional to position size, not per-unit price
        entry_fee_cents = int(quantity_cents * fee_rate)

        self.cash_cents -= quantity_cents
        self.total_fees_cents += entry_fee_cents

        self._position = _Position(
            symbol=signal.symbol,
            market=market,
            entry_price_cents=fill_price,
            quantity_cents=quantity_cents,
            stop_loss_cents=signal.stop_loss_cents,
            strategy=signal.strategy_name,
            entry_time=bar_time,
            entry_bar_index=bar_index,
        )

    def _add_to_position(
        self, signal: Signal, bar_time: datetime, bar_index: int, market: str
    ) -> None:
        """Add to an existing position (pyramiding).

        Calculates weighted-average entry price, adds quantity, and updates
        the stop-loss from the new signal. Mirrors PaperExecutor._open_or_add_position.
        """
        pos = self._position
        if pos is None:
            return

        # Apply entry fee
        fee_rate = self._get_fee_rate(market)
        fill_price = int(signal.price_cents * (1 + fee_rate))

        add_quantity = signal.indicator_data.get("quantity_cents", 100)
        add_quantity = min(add_quantity, self.max_position_size_cents)
        add_quantity = min(add_quantity, self.cash_cents)

        if add_quantity < 100:  # Minimum $1 addition
            return

        entry_fee_cents = int(add_quantity * fee_rate)

        # Weighted-average entry price
        old_total = pos.quantity_cents
        new_total = old_total + add_quantity
        pos.entry_price_cents = int(
            (pos.entry_price_cents * old_total + fill_price * add_quantity) / new_total
        )
        pos.quantity_cents = new_total
        pos.pyramid_count += 1

        # Update stop-loss from signal (turtle ratchets stops up)
        if signal.stop_loss_cents > 0:
            pos.stop_loss_cents = signal.stop_loss_cents

        self.cash_cents -= add_quantity
        self.total_fees_cents += entry_fee_cents

        logger.debug(
            "Pyramid #%d on %s: +%d cents @ %d, new avg entry %d, stop %d",
            pos.pyramid_count, pos.symbol, add_quantity, fill_price,
            pos.entry_price_cents, pos.stop_loss_cents,
        )

    def _close_position(
        self,
        price_cents: int,
        bar_time: datetime,
        bar_index: int,
        reason: str,
        market: str,
    ) -> None:
        """Close the current position and record the trade."""
        if self._position is None:
            return

        pos = self._position

        # Track last close bar for cooldown
        self._last_close_bar_index = bar_index

        # Apply exit fee (selling at slightly lower price due to spread)
        fee_rate = self._get_fee_rate(market)
        fill_price = int(price_cents * (1 - fee_rate))

        # Fee is proportional to position size, not per-unit price
        exit_fee_cents = int(pos.quantity_cents * fee_rate)

        # P&L formula matching PaperExecutor: quantity * (exit - entry) / entry
        if pos.entry_price_cents > 0:
            pnl_cents = int(
                pos.quantity_cents * (fill_price - pos.entry_price_cents) / pos.entry_price_cents
            )
        else:
            pnl_cents = 0

        # Return capital + profit (or minus loss) to cash
        self.cash_cents += pos.quantity_cents + pnl_cents
        self.total_fees_cents += exit_fee_cents

        holding_bars = bar_index - pos.entry_bar_index

        self.closed_trades.append(
            ClosedTrade(
                symbol=pos.symbol,
                market=pos.market,
                entry_price_cents=pos.entry_price_cents,
                exit_price_cents=fill_price,
                quantity_cents=pos.quantity_cents,
                pnl_cents=pnl_cents,
                entry_time=pos.entry_time,
                exit_time=bar_time,
                exit_reason=reason,
                strategy=pos.strategy,
                holding_bars=holding_bars,
            )
        )

        self._position = None

    def _record_equity(self, bar_close_cents: int, bar_time: datetime) -> None:
        """Snapshot current equity (cash + position mark-to-market)."""
        equity = self.get_equity_cents(bar_close_cents)
        self.equity_curve.append({
            "timestamp": bar_time.isoformat(),
            "equity_cents": equity,
            "cash_cents": self.cash_cents,
        })

    def get_equity_cents(self, mark_price_cents: int) -> int:
        """Total equity = cash + position value at mark price."""
        if self._position is None:
            return self.cash_cents

        pos = self._position
        if pos.entry_price_cents > 0:
            position_value = int(
                pos.quantity_cents * mark_price_cents / pos.entry_price_cents
            )
        else:
            position_value = pos.quantity_cents

        return self.cash_cents + position_value

    def force_close(
        self, price_cents: int, bar_time: datetime, bar_index: int, market: str
    ) -> None:
        """Force-close any open position (used at end of backtest)."""
        if self._position is not None:
            self._close_position(price_cents, bar_time, bar_index, "backtest_end", market)
