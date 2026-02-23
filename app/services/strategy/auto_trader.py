"""AutoTrader orchestrator — evaluates symbols, picks strategies, sizes positions.

This is the brain that ties together regime detection, strategies, and position sizing.
Auto-trade state is stored in Redis for cross-process access (FastAPI + Celery).
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.strategy.base import Signal
from app.services.strategy.indicators import atr
from app.services.strategy.meanrev import MeanReversionStrategy
from app.services.strategy.momentum import MomentumStrategy
from app.services.strategy.regime import RegimeType, detect_regime

logger = logging.getLogger(__name__)

# Recommended symbols for auto-trading
WATCHED_SYMBOLS = [
    # Crypto — high liquidity, 24/7, good for both strategies
    {"symbol": "BTC", "market": "crypto", "timeframe": "1h"},
    {"symbol": "ETH", "market": "crypto", "timeframe": "1h"},
    {"symbol": "SOL", "market": "crypto", "timeframe": "1h"},
    {"symbol": "XRP", "market": "crypto", "timeframe": "1h"},
    {"symbol": "DOGE", "market": "crypto", "timeframe": "1h"},
    # ASX — blue chips, liquid, good for mean reversion
    {"symbol": "BHP.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "CBA.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "CSL.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "WDS.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "FMG.AX", "market": "asx", "timeframe": "1d"},
]

REDIS_KEY_AUTO_TRADE = "flashtrade:auto_trade"
REDIS_KEY_REGIME_PREFIX = "flashtrade:regime:"
REDIS_KEY_LAST_SIGNAL_PREFIX = "flashtrade:signal:"


class AutoTrader:
    """Orchestrates strategy selection and signal generation for watched symbols."""

    def __init__(self) -> None:
        self._momentum = MomentumStrategy()
        self._meanrev = MeanReversionStrategy()
        self._redis: aioredis.Redis | None = None
        self._portfolio_value_cents = 1_000_000  # $10,000

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, decode_responses=True,
                max_connections=5,
            )
        return self._redis

    async def close(self) -> None:
        """Close Redis connection pool."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def is_enabled(self) -> bool:
        """Check if auto-trading is enabled."""
        r = await self._get_redis()
        val = await r.get(REDIS_KEY_AUTO_TRADE)
        return val == "1"

    async def set_enabled(self, enabled: bool) -> None:
        """Enable or disable auto-trading."""
        r = await self._get_redis()
        await r.set(REDIS_KEY_AUTO_TRADE, "1" if enabled else "0")
        logger.info("Auto-trade %s", "enabled" if enabled else "disabled")

    async def get_status(self) -> dict:
        """Get auto-trade status with regime info for all watched symbols.

        Detects regimes on-demand if no cached data exists, so the dashboard
        always shows useful info even before auto-trade is enabled.
        """
        r = await self._get_redis()
        enabled = await r.get(REDIS_KEY_AUTO_TRADE) == "1"

        symbols_status = []
        for sym in WATCHED_SYMBOLS:
            regime = await r.get(f"{REDIS_KEY_REGIME_PREFIX}{sym['symbol']}")
            last_signal = await r.get(f"{REDIS_KEY_LAST_SIGNAL_PREFIX}{sym['symbol']}")

            # Detect regime on-demand if not cached
            if not regime:
                try:
                    df = await self._load_ohlcv(sym["symbol"], sym["timeframe"], lookback_days=60)
                    if df is not None and len(df) >= 30:
                        detected = detect_regime(df)
                        regime = detected.value
                        await r.set(f"{REDIS_KEY_REGIME_PREFIX}{sym['symbol']}", regime, ex=3600)
                except Exception:
                    regime = None

            symbols_status.append({
                "symbol": sym["symbol"],
                "market": sym["market"],
                "timeframe": sym["timeframe"],
                "regime": regime or "unknown",
                "active_strategy": self._strategy_for_regime(regime).name if regime else "none",
                "last_signal": last_signal or "hold",
            })

        last_evaluated = await r.get("flashtrade:last_evaluated_at")

        return {
            "enabled": enabled,
            "symbols": symbols_status,
            "portfolio_value_cents": self._portfolio_value_cents,
            "last_evaluated_at": last_evaluated,
            "evaluate_interval_seconds": 300,
        }

    def _strategy_for_regime(self, regime: str | None) -> MomentumStrategy | MeanReversionStrategy:
        """Pick strategy based on regime. Defaults to mean reversion for safety."""
        if regime == RegimeType.TRENDING.value:
            return self._momentum
        return self._meanrev

    async def evaluate_symbol(self, symbol: str, market: str, timeframe: str = "1h") -> Signal | None:
        """Evaluate a single symbol and return a signal if one is generated.

        Steps:
        1. Pull recent OHLCV from DB
        2. Detect market regime
        3. Pick strategy based on regime
        4. Generate signal
        5. Apply position sizing
        """
        df = await self._load_ohlcv(symbol, timeframe, lookback_days=60)
        if df is None or len(df) < 30:
            logger.warning("Insufficient data for %s (%s), skipping", symbol, len(df) if df is not None else 0)
            return None

        # Detect regime
        regime = detect_regime(df)
        r = await self._get_redis()
        await r.set(f"{REDIS_KEY_REGIME_PREFIX}{symbol}", regime.value, ex=3600)
        logger.info("Symbol %s regime: %s", symbol, regime.value)

        # Pick strategy
        strategy = self._strategy_for_regime(regime.value)
        logger.info("Symbol %s using strategy: %s", symbol, strategy.name)

        # Generate signals
        signals = strategy.generate_signals(df, symbol, market)
        if not signals:
            await r.set(f"{REDIS_KEY_LAST_SIGNAL_PREFIX}{symbol}", "hold", ex=3600)
            return None

        # Take the strongest signal
        best = max(signals, key=lambda s: s.strength)

        # Apply position sizing (risk 1% of portfolio per trade)
        best = self._apply_position_sizing(best, df)

        # Cache latest signal
        await r.set(
            f"{REDIS_KEY_LAST_SIGNAL_PREFIX}{symbol}",
            f"{best.action}@{best.price_cents}",
            ex=3600,
        )

        logger.info(
            "Signal generated: %s %s @ %d cents (strategy=%s, strength=%.2f)",
            best.action, best.symbol, best.price_cents, best.strategy_name, best.strength,
        )
        return best

    def _apply_position_sizing(self, signal: Signal, df: pd.DataFrame) -> Signal:
        """Size the position based on ATR and portfolio risk budget.

        Risk per trade = 1% of portfolio.
        Quantity = risk_budget / stop_distance.
        Capped at max_position_size_cents.
        """
        risk_budget_cents = int(self._portfolio_value_cents * 0.01)  # 1% risk
        stop_distance = abs(signal.price_cents - signal.stop_loss_cents)

        if stop_distance <= 0:
            # Fallback: use ATR for stop distance
            atr_values = atr(df["high"], df["low"], df["close"])
            current_atr = atr_values.iloc[-1]
            stop_distance = int(current_atr * 2) if not pd.isna(current_atr) else signal.price_cents // 20

        if stop_distance <= 0:
            stop_distance = max(1, signal.price_cents // 20)

        # quantity_cents = how much AUD to spend on this position
        # Risk = quantity * (stop_distance / price), so quantity = risk * price / stop_distance
        if signal.price_cents > 0:
            quantity_cents = int(risk_budget_cents * signal.price_cents / stop_distance)
        else:
            quantity_cents = risk_budget_cents

        # Cap at max position size
        quantity_cents = min(quantity_cents, settings.max_position_size_cents)
        # Floor at $1
        quantity_cents = max(100, quantity_cents)

        signal.indicator_data["quantity_cents"] = quantity_cents
        signal.indicator_data["risk_budget_cents"] = risk_budget_cents
        return signal

    async def _load_ohlcv(self, symbol: str, timeframe: str, lookback_days: int = 60) -> pd.DataFrame | None:
        """Load OHLCV data from database into a pandas DataFrame."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        async with async_session() as session:
            stmt = (
                select(OHLCV)
                .where(
                    OHLCV.symbol == symbol,
                    OHLCV.timeframe == timeframe,
                    OHLCV.timestamp >= cutoff,
                )
                .order_by(OHLCV.timestamp.asc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        if not rows:
            return None

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
        return df
