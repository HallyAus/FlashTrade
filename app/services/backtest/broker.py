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
    "crypto": 0.004,  # 0.4% Swyftx spread
    "asx": 0.001,  # 0.1% brokerage estimate
    "us": 0.001,  # 0.1% Alpaca spread estimate
}


class _Position:
    """Internal position tracker during backtest."""

    __slots__ = (
        "symbol", "market", "entry_price_cents", "quantity_cents",
        "stop_loss_cents", "strategy", "entry_time", "entry_bar_index",
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
    ) -> None:
        self.cash_cents: int = starting_cash_cents
        self.starting_cash_cents: int = starting_cash_cents
        self.max_position_size_cents = max_position_size_cents
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

        # 3. Record equity
        self._record_equity(bar_close_cents, bar_time)

    def _open_position(
        self, signal: Signal, bar_time: datetime, bar_index: int, market: str
    ) -> None:
        """Open a new position from a buy signal."""
        # Apply entry fee (buying at slightly higher price due to spread)
        fee_rate = FEE_RATES.get(market, 0.001)
        fill_price = int(signal.price_cents * (1 + fee_rate))
        fee_cents = fill_price - signal.price_cents

        # Position size from signal (already sized by engine)
        quantity_cents = signal.indicator_data.get("quantity_cents", 100)
        quantity_cents = min(quantity_cents, self.max_position_size_cents)
        quantity_cents = min(quantity_cents, self.cash_cents)  # Can't spend more than we have

        if quantity_cents < 100:  # Minimum $1 position
            return

        self.cash_cents -= quantity_cents
        self.total_fees_cents += fee_cents

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

        # Apply exit fee (selling at slightly lower price due to spread)
        fee_rate = FEE_RATES.get(market, 0.001)
        fill_price = int(price_cents * (1 - fee_rate))
        fee_cents = price_cents - fill_price

        # P&L formula matching PaperExecutor: quantity * (exit - entry) / entry
        if pos.entry_price_cents > 0:
            pnl_cents = int(
                pos.quantity_cents * (fill_price - pos.entry_price_cents) / pos.entry_price_cents
            )
        else:
            pnl_cents = 0

        # Return capital + profit (or minus loss) to cash
        self.cash_cents += pos.quantity_cents + pnl_cents
        self.total_fees_cents += fee_cents

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
