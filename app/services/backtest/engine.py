"""Walk-forward backtesting engine. Calls existing strategies on expanding windows."""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.backtest.broker import BacktestBroker
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.result import BacktestResult
from app.services.strategy.base import BaseStrategy, Signal
from app.services.strategy.indicators import atr
from app.services.strategy.meanrev import MeanReversionStrategy
from app.services.strategy.momentum import MomentumStrategy
from app.services.strategy.regime import RegimeType, detect_regime

logger = logging.getLogger(__name__)

# Minimum warmup bars for indicator stability (RSI=14, MACD slow=26, BB=20)
MIN_WARMUP_BARS = 30

STRATEGY_MAP: dict[str, type[BaseStrategy]] = {
    "momentum": MomentumStrategy,
    "meanrev": MeanReversionStrategy,
}


class BacktestEngine:
    """Walk-forward backtesting engine.

    Iterates through historical bars one at a time. At each bar,
    provides the strategy with all data up to (and including) that bar,
    simulating what the strategy would see in real time.

    Supports:
    - Fixed strategy: always use the specified strategy
    - Auto mode: use regime detection to switch between strategies
      (mirrors AutoTrader behavior)
    """

    def __init__(
        self,
        strategy_name: str,
        symbol: str,
        market: str,
        timeframe: str = "1h",
        days: int = 180,
        starting_cash_cents: int = 1_000_000,
        auto_regime: bool = False,
        strategy_params: dict | None = None,
        fee_tier: str = "default",
        cooldown_bars: int = 0,
    ) -> None:
        self._strategy_name = strategy_name
        self._symbol = symbol
        self._market = market
        self._timeframe = timeframe
        self._days = days
        self._starting_cash_cents = starting_cash_cents
        self._auto_regime = auto_regime
        self._strategy_params = strategy_params or {}
        self._fee_tier = fee_tier
        self._cooldown_bars = cooldown_bars

    async def run(self) -> BacktestResult:
        """Execute the backtest.

        Steps:
        1. Load OHLCV data from database
        2. Validate sufficient data
        3. Walk forward through bars
        4. Force-close any open position at end
        5. Compute metrics
        """
        df = await self._load_data()

        if len(df) < MIN_WARMUP_BARS + 10:
            raise ValueError(
                f"Insufficient data for {self._symbol}: {len(df)} bars "
                f"(need at least {MIN_WARMUP_BARS + 10}). "
                f"Run backfill first."
            )

        # Auto mode starts with meanrev, switches based on regime detection
        if self._auto_regime:
            strategy: BaseStrategy = MeanReversionStrategy(**self._strategy_params)
        else:
            strategy = STRATEGY_MAP[self._strategy_name](**self._strategy_params)
        broker = BacktestBroker(
            starting_cash_cents=self._starting_cash_cents,
            max_position_size_cents=settings.max_position_size_cents,
            cooldown_bars=self._cooldown_bars,
            fee_tier=self._fee_tier,
        )

        logger.info(
            "Starting backtest: %s on %s (%s, %s), %d bars",
            self._strategy_name, self._symbol, self._market,
            self._timeframe, len(df),
        )

        for i in range(MIN_WARMUP_BARS, len(df)):
            window = df.iloc[: i + 1]  # Expanding window — no look-ahead bias
            bar = df.iloc[i]

            # Optionally switch strategy based on regime (every 20 bars)
            if self._auto_regime and i % 20 == 0:
                regime = detect_regime(window)
                strategy = self._strategy_for_regime(regime)

            # Generate signals (same call as live trading)
            signals = strategy.generate_signals(window, self._symbol, self._market)

            # Take strongest signal (same as AutoTrader)
            best_signal: Signal | None = None
            if signals:
                best_signal = max(signals, key=lambda s: s.strength)

                # Skip sell signals when we don't have a position
                if best_signal.action == "sell" and not broker.has_position:
                    best_signal = None

                # Apply position sizing for buy signals
                if best_signal is not None and best_signal.action == "buy":
                    portfolio_value = broker.get_equity_cents(int(bar["close"]))
                    best_signal = self._apply_position_sizing(
                        best_signal, window, portfolio_value
                    )

            # Process bar through broker
            broker.process_bar(
                signal=best_signal,
                bar_high_cents=int(bar["high"]),
                bar_low_cents=int(bar["low"]),
                bar_close_cents=int(bar["close"]),
                bar_time=window.index[i],
                bar_index=i,
                market=self._market,
            )

        # Force-close any open position at last bar
        if broker.has_position:
            broker.force_close(
                price_cents=int(df.iloc[-1]["close"]),
                bar_time=df.index[-1],
                bar_index=len(df) - 1,
                market=self._market,
            )

        # Compute all metrics
        result = compute_metrics(
            broker=broker,
            strategy_name=self._strategy_name if not self._auto_regime else "auto",
            symbol=self._symbol,
            market=self._market,
            timeframe=self._timeframe,
            start_date=str(df.index[MIN_WARMUP_BARS]),
            end_date=str(df.index[-1]),
            bars_processed=len(df) - MIN_WARMUP_BARS,
        )

        logger.info(
            "Backtest complete: %s on %s — return=%.2f%%, sharpe=%.2f, "
            "trades=%d, win_rate=%.1f%%",
            result.strategy_name, result.symbol, result.total_return_pct,
            result.sharpe_ratio, result.total_trades, result.win_rate_pct,
        )
        return result

    async def _load_data(self) -> pd.DataFrame:
        """Load OHLCV data from PostgreSQL. Mirrors AutoTrader._load_ohlcv()."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)

        async with async_session() as session:
            stmt = (
                select(OHLCV)
                .where(
                    OHLCV.symbol == self._symbol,
                    OHLCV.timeframe == self._timeframe,
                    OHLCV.timestamp >= cutoff,
                )
                .order_by(OHLCV.timestamp.asc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        if not rows:
            raise ValueError(
                f"No OHLCV data for {self._symbol} ({self._timeframe}) "
                f"in the last {self._days} days. Run backfill first."
            )

        data = {
            "timestamp": [r.timestamp for r in rows],
            "open": [float(r.open) for r in rows],
            "high": [float(r.high) for r in rows],
            "low": [float(r.low) for r in rows],
            "close": [float(r.close) for r in rows],
            "volume": [float(r.volume) for r in rows],
        }
        df = pd.DataFrame(data)
        df.set_index("timestamp", inplace=True)

        logger.info(
            "Loaded %d bars for %s (%s) from %s to %s",
            len(df), self._symbol, self._timeframe,
            df.index[0], df.index[-1],
        )
        return df

    def _strategy_for_regime(self, regime: RegimeType) -> BaseStrategy:
        """Pick strategy based on regime. Same logic as AutoTrader."""
        if regime == RegimeType.TRENDING:
            return MomentumStrategy(**self._strategy_params)
        return MeanReversionStrategy(**self._strategy_params)

    def _apply_position_sizing(
        self, signal: Signal, df: pd.DataFrame, portfolio_value_cents: int
    ) -> Signal:
        """Size the position. Replicates AutoTrader._apply_position_sizing()."""
        risk_budget_cents = int(portfolio_value_cents * 0.01)  # 1% risk
        stop_distance = abs(signal.price_cents - signal.stop_loss_cents)

        if stop_distance <= 0:
            atr_values = atr(df["high"], df["low"], df["close"])
            current_atr = atr_values.iloc[-1]
            stop_distance = (
                int(current_atr * 2)
                if not pd.isna(current_atr)
                else signal.price_cents // 20
            )

        if stop_distance <= 0:
            stop_distance = max(1, signal.price_cents // 20)

        if signal.price_cents > 0:
            quantity_cents = int(risk_budget_cents * signal.price_cents / stop_distance)
        else:
            quantity_cents = risk_budget_cents

        quantity_cents = min(quantity_cents, settings.max_position_size_cents)
        quantity_cents = max(100, quantity_cents)

        signal.indicator_data["quantity_cents"] = quantity_cents
        signal.indicator_data["risk_budget_cents"] = risk_budget_cents
        return signal
