"""AutoTrader orchestrator — evaluates symbols, picks strategies, sizes positions.

This is the brain that ties together regime detection, strategies, and position sizing.
Auto-trade state is stored in Redis for cross-process access (FastAPI + Celery).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.strategy.base import Signal
from app.services.strategy.indicators import atr, bollinger_bands, macd, rsi
from app.services.strategy.meanrev import MeanReversionStrategy
from app.services.strategy.momentum import MomentumStrategy
from app.services.strategy.regime import RegimeType, detect_regime

logger = logging.getLogger(__name__)

# Recommended symbols for auto-trading (30 total: 10 crypto, 10 ASX, 10 US)
WATCHED_SYMBOLS = [
    # Crypto — high liquidity, 24/7, good for both strategies
    {"symbol": "BTC", "market": "crypto", "timeframe": "1h"},
    {"symbol": "ETH", "market": "crypto", "timeframe": "1h"},
    {"symbol": "SOL", "market": "crypto", "timeframe": "1h"},
    {"symbol": "XRP", "market": "crypto", "timeframe": "1h"},
    {"symbol": "DOGE", "market": "crypto", "timeframe": "1h"},
    {"symbol": "ADA", "market": "crypto", "timeframe": "1h"},
    {"symbol": "AVAX", "market": "crypto", "timeframe": "1h"},
    {"symbol": "LINK", "market": "crypto", "timeframe": "1h"},
    {"symbol": "DOT", "market": "crypto", "timeframe": "1h"},
    {"symbol": "POL", "market": "crypto", "timeframe": "1h"},
    # ASX — blue chips, liquid, good for mean reversion
    {"symbol": "BHP.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "CBA.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "CSL.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "WDS.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "FMG.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "NAB.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "WBC.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "ANZ.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "WOW.AX", "market": "asx", "timeframe": "1d"},
    {"symbol": "RIO.AX", "market": "asx", "timeframe": "1d"},
    # US — NASDAQ/large-cap tech + QQQ index
    {"symbol": "AAPL", "market": "us", "timeframe": "1d"},
    {"symbol": "NVDA", "market": "us", "timeframe": "1d"},
    {"symbol": "MSFT", "market": "us", "timeframe": "1d"},
    {"symbol": "GOOGL", "market": "us", "timeframe": "1d"},
    {"symbol": "AMZN", "market": "us", "timeframe": "1d"},
    {"symbol": "META", "market": "us", "timeframe": "1d"},
    {"symbol": "TSLA", "market": "us", "timeframe": "1d"},
    {"symbol": "AMD", "market": "us", "timeframe": "1d"},
    {"symbol": "NFLX", "market": "us", "timeframe": "1d"},
    {"symbol": "QQQ", "market": "us", "timeframe": "1d"},
]

REDIS_KEY_AUTO_TRADE = "flashtrade:auto_trade"
REDIS_KEY_REGIME_PREFIX = "flashtrade:regime:"
REDIS_KEY_LAST_SIGNAL_PREFIX = "flashtrade:signal:"
REDIS_KEY_PROXIMITY_PREFIX = "flashtrade:proximity:"


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

        Recomputes regime and proximity on-demand if cache is expired.
        Proximity cache is short (5 min) so dashboard always shows fresh data.
        """
        r = await self._get_redis()
        enabled = await r.get(REDIS_KEY_AUTO_TRADE) == "1"

        symbols_status = []
        for sym in WATCHED_SYMBOLS:
            regime = await r.get(f"{REDIS_KEY_REGIME_PREFIX}{sym['symbol']}")
            last_signal = await r.get(f"{REDIS_KEY_LAST_SIGNAL_PREFIX}{sym['symbol']}")
            proximity_raw = await r.get(f"{REDIS_KEY_PROXIMITY_PREFIX}{sym['symbol']}")

            # Recompute if regime OR proximity cache is expired
            if not regime or not proximity_raw:
                try:
                    df = await self._load_ohlcv(sym["symbol"], sym["timeframe"], lookback_days=60)
                    if df is not None and len(df) >= 30:
                        detected = detect_regime(df)
                        regime = detected.value
                        await r.set(f"{REDIS_KEY_REGIME_PREFIX}{sym['symbol']}", regime, ex=3600)
                        strat_name = self._strategy_for_regime(regime).name
                        prox = self._compute_proximity(df, strat_name)
                        proximity_raw = json.dumps(prox)
                        await r.set(f"{REDIS_KEY_PROXIMITY_PREFIX}{sym['symbol']}", proximity_raw, ex=300)
                except Exception:
                    regime = regime or None

            proximity = json.loads(proximity_raw) if proximity_raw else None

            symbols_status.append({
                "symbol": sym["symbol"],
                "market": sym["market"],
                "timeframe": sym["timeframe"],
                "regime": regime or "unknown",
                "active_strategy": self._strategy_for_regime(regime).name if regime else "none",
                "last_signal": last_signal or "hold",
                "proximity": proximity,
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

        # Compute and cache signal proximity (how close to a buy trigger)
        # Short TTL (5 min) so dashboard always shows fresh indicator values
        proximity = self._compute_proximity(df, strategy.name)
        await r.set(
            f"{REDIS_KEY_PROXIMITY_PREFIX}{symbol}",
            json.dumps(proximity),
            ex=300,
        )

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

    def _compute_proximity(self, df: pd.DataFrame, strategy_name: str) -> dict:
        """Compute how close current indicators are to triggering a buy signal.

        Returns a dict with conditions, each showing current value, target, and whether met.
        """
        close = df["close"]
        rsi_values = rsi(close)
        current_rsi = float(rsi_values.iloc[-1]) if not pd.isna(rsi_values.iloc[-1]) else None
        prev_rsi = float(rsi_values.iloc[-2]) if len(rsi_values) >= 2 and not pd.isna(rsi_values.iloc[-2]) else None

        if strategy_name == "momentum":
            _, _, macd_hist = macd(close)
            current_hist = float(macd_hist.iloc[-1]) if not pd.isna(macd_hist.iloc[-1]) else None
            prev_hist = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 and not pd.isna(macd_hist.iloc[-2]) else None

            conditions = []

            # Condition 1: RSI was below 30 (setup)
            rsi_below_30 = prev_rsi is not None and prev_rsi < 30
            conditions.append({
                "name": "RSI below 30 (setup)",
                "current": round(prev_rsi, 1) if prev_rsi is not None else None,
                "target": 30,
                "met": rsi_below_30,
                "direction": "below",
            })

            # Condition 2: RSI crosses above 30 (trigger)
            rsi_cross = rsi_below_30 and current_rsi is not None and current_rsi >= 30
            conditions.append({
                "name": "RSI crosses above 30",
                "current": round(current_rsi, 1) if current_rsi is not None else None,
                "target": 30,
                "met": rsi_cross,
                "direction": "above",
            })

            # Condition 3: MACD histogram turns positive
            hist_positive = (
                prev_hist is not None
                and current_hist is not None
                and prev_hist <= 0
                and current_hist > 0
            )
            conditions.append({
                "name": "MACD histogram turns positive",
                "current": round(current_hist, 0) if current_hist is not None else None,
                "target": 0,
                "met": hist_positive,
                "direction": "above",
            })

            met_count = sum(1 for c in conditions if c["met"])
            return {
                "strategy": "momentum",
                "conditions": conditions,
                "conditions_met": met_count,
                "conditions_total": len(conditions),
                "rsi": round(current_rsi, 1) if current_rsi is not None else None,
                "macd_hist": round(current_hist, 0) if current_hist is not None else None,
            }

        else:  # meanrev
            upper, middle, lower, _ = bollinger_bands(close)
            current_close = float(close.iloc[-1])
            current_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else None
            current_middle = float(middle.iloc[-1]) if not pd.isna(middle.iloc[-1]) else None

            conditions = []

            # Condition 1: Price below lower BB
            below_bb = current_lower is not None and current_close < current_lower
            bb_distance_pct = None
            if current_lower and current_lower > 0:
                bb_distance_pct = round((current_close - current_lower) / current_lower * 100, 2)
            conditions.append({
                "name": "Price below lower BB",
                "current": round(current_close, 0),
                "target": round(current_lower, 0) if current_lower else None,
                "met": below_bb,
                "direction": "below",
                "distance_pct": bb_distance_pct,
            })

            # Condition 2: RSI < 35
            rsi_oversold = current_rsi is not None and current_rsi < 35
            conditions.append({
                "name": "RSI below 35 (oversold)",
                "current": round(current_rsi, 1) if current_rsi is not None else None,
                "target": 35,
                "met": rsi_oversold,
                "direction": "below",
            })

            met_count = sum(1 for c in conditions if c["met"])
            return {
                "strategy": "meanrev",
                "conditions": conditions,
                "conditions_met": met_count,
                "conditions_total": len(conditions),
                "rsi": round(current_rsi, 1) if current_rsi is not None else None,
                "price": round(current_close, 0),
                "bb_lower": round(current_lower, 0) if current_lower else None,
                "bb_middle": round(current_middle, 0) if current_middle else None,
            }

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
