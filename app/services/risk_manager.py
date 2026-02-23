"""Risk manager — ALL trades must pass through here before execution.

This is the most critical module. No trade reaches a broker without approval.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Order:
    """Proposed order to be evaluated by the risk manager."""

    symbol: str
    market: str
    side: str  # buy, sell
    order_type: str  # market, limit, stop
    quantity_cents: int  # position size in cents
    price_cents: int  # expected price in cents
    stop_loss_cents: int  # mandatory
    strategy: str
    reason: str


@dataclass
class RiskVerdict:
    """Result of risk evaluation."""

    approved: bool
    reason: str
    adjusted_quantity_cents: int | None = None


class RiskManager:
    """Evaluates every trade against portfolio risk rules.

    Rules enforced:
    - Max position size ($100 / 10000 cents)
    - Max 2% portfolio risk per trade
    - Max 5% daily drawdown → auto-halt
    - Circuit breaker: 3 consecutive losses → pause 1 hour
    - Every order must have a stop-loss
    """

    def __init__(self) -> None:
        self._consecutive_losses: int = 0
        self._halted: bool = False
        self._halt_reason: str = ""
        self._paused_until: datetime | None = None
        self._daily_pnl_cents: int = 0
        self._portfolio_value_cents: int = 1_000_000  # $10,000 default

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        if datetime.now(timezone.utc) >= self._paused_until:
            self._paused_until = None
            self._consecutive_losses = 0
            logger.info("Circuit breaker pause expired, resuming trading")
            return False
        return True

    def evaluate(self, order: Order) -> RiskVerdict:
        """Evaluate an order against all risk rules. Returns approval or rejection."""

        # Check halt state
        if self._halted:
            return RiskVerdict(
                approved=False,
                reason=f"Trading halted: {self._halt_reason}",
            )

        # Check circuit breaker pause
        if self.is_paused:
            remaining = self._paused_until - datetime.now(timezone.utc)
            return RiskVerdict(
                approved=False,
                reason=f"Circuit breaker active. Resuming in {remaining.seconds // 60}m",
            )

        # Rule: every order must have a stop-loss
        if order.stop_loss_cents <= 0:
            return RiskVerdict(
                approved=False,
                reason="Order rejected: stop-loss is mandatory",
            )

        # Rule: max position size
        if order.quantity_cents > settings.max_position_size_cents:
            return RiskVerdict(
                approved=False,
                reason=(
                    f"Position size {order.quantity_cents} cents exceeds max "
                    f"{settings.max_position_size_cents} cents"
                ),
            )

        # Rule: max per-trade risk (scaled to position size)
        if order.price_cents > 0:
            stop_distance_pct = abs(order.price_cents - order.stop_loss_cents) / order.price_cents
            risk_per_trade_cents = int(order.quantity_cents * stop_distance_pct)
        else:
            risk_per_trade_cents = order.quantity_cents
        max_risk_cents = int(
            self._portfolio_value_cents * settings.max_per_trade_risk_pct / 100
        )
        if risk_per_trade_cents > max_risk_cents:
            return RiskVerdict(
                approved=False,
                reason=(
                    f"Per-trade risk {risk_per_trade_cents} cents exceeds "
                    f"max {max_risk_cents} cents ({settings.max_per_trade_risk_pct}% of portfolio)"
                ),
            )

        # Rule: daily drawdown check
        max_daily_loss_cents = int(
            self._portfolio_value_cents * settings.max_daily_drawdown_pct / 100
        )
        if abs(self._daily_pnl_cents) >= max_daily_loss_cents and self._daily_pnl_cents < 0:
            self._halted = True
            self._halt_reason = (
                f"Daily drawdown limit hit: {self._daily_pnl_cents} cents "
                f"(max {max_daily_loss_cents} cents)"
            )
            return RiskVerdict(approved=False, reason=self._halt_reason)

        logger.info(
            "Order approved: %s %s %s @ %d cents, SL @ %d cents",
            order.side,
            order.symbol,
            order.quantity_cents,
            order.price_cents,
            order.stop_loss_cents,
        )
        return RiskVerdict(approved=True, reason="All risk checks passed")

    def record_trade_result(self, pnl_cents: int) -> None:
        """Record a trade result for circuit breaker and daily P&L tracking."""
        self._daily_pnl_cents += pnl_cents

        if pnl_cents < 0:
            self._consecutive_losses += 1
            logger.warning(
                "Loss recorded: %d cents. Consecutive losses: %d",
                pnl_cents,
                self._consecutive_losses,
            )
            if self._consecutive_losses >= settings.circuit_breaker_consecutive_losses:
                self._paused_until = datetime.now(timezone.utc) + timedelta(
                    minutes=settings.circuit_breaker_pause_minutes
                )
                logger.warning(
                    "Circuit breaker triggered! Pausing until %s",
                    self._paused_until.isoformat(),
                )
        else:
            self._consecutive_losses = 0

    def kill_switch(self) -> None:
        """Emergency halt — stops all trading immediately."""
        self._halted = True
        self._halt_reason = "Kill switch activated manually"
        logger.critical("KILL SWITCH ACTIVATED — all trading halted")

    def reset_halt(self) -> None:
        """Reset halt state (use with caution)."""
        self._halted = False
        self._halt_reason = ""
        self._consecutive_losses = 0
        logger.info("Trading halt reset")

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counter (call at start of each trading day)."""
        self._daily_pnl_cents = 0

    def set_portfolio_value(self, value_cents: int) -> None:
        """Update portfolio value for risk calculations."""
        self._portfolio_value_cents = value_cents
