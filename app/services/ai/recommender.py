"""Claude AI recommendation engine — analyzes market data and generates trading ideas.

Gathers technical indicators, regime data, and prices for all watched symbols,
sends a condensed summary to Claude Sonnet, and parses structured JSON recommendations.
Results are cached in Redis for instant dashboard access.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum

import anthropic
import pandas as pd
import redis.asyncio as aioredis
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.strategy.auto_trader import get_watched_symbols
from app.services.strategy.indicators import (
    adx,
    atr,
    bollinger_bands,
    donchian_channel,
    macd,
    rsi,
)

logger = logging.getLogger(__name__)

REDIS_KEY_RECOMMENDATIONS = "flashtrade:recommendations"
REDIS_KEY_RECOMMENDATIONS_ERROR = "flashtrade:recommendations:last_error"
REDIS_KEY_MARKET_OVERVIEW = "flashtrade:market_overview"

SYSTEM_PROMPT = """Quantitative trading analyst. Analyze indicators and return JSON only.

Return EXACTLY 3 items per market array (12 total). Use "watch"/"hold" for weak setups.
All prices in cents. Max $100/position, 2% risk, $10K portfolio. Keep reasoning to 1 sentence.

JSON schema (no markdown wrapping):
{"market_summary":"1-2 sentences","crypto_opportunities":[{"symbol":"BTC","market":"crypto","action":"buy|sell|hold|watch","confidence":0.75,"current_price_cents":15000000,"entry_price_cents":14800000,"target_price_cents":16000000,"stop_loss_cents":14200000,"reasoning":"1 sentence","risk_notes":"1 sentence","timeframe":"1-3 days"}],"asx_opportunities":[...],"us_opportunities":[...],"uk_opportunities":[...],"symbols_to_avoid":["DOGE"]}"""


class RecommendationAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WATCH = "watch"


class Recommendation(BaseModel):
    """Single symbol recommendation from Claude analysis."""

    symbol: str
    market: str
    action: RecommendationAction
    confidence: float = Field(ge=0.0, le=1.0)
    current_price_cents: int
    entry_price_cents: int | None = None
    target_price_cents: int | None = None
    stop_loss_cents: int | None = None
    reasoning: str
    risk_notes: str
    timeframe: str = ""


class RecommendationSet(BaseModel):
    """Complete set of recommendations from one Claude analysis."""

    generated_at_utc: str
    model_used: str
    market_summary: str
    top_opportunities: list[Recommendation]
    crypto_opportunities: list[Recommendation] = Field(default_factory=list)
    asx_opportunities: list[Recommendation] = Field(default_factory=list)
    us_opportunities: list[Recommendation] = Field(default_factory=list)
    uk_opportunities: list[Recommendation] = Field(default_factory=list)
    market_overview: list[dict] = Field(default_factory=list)
    symbols_to_avoid: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "AI-generated analysis for informational purposes only. "
        "Not financial advice. Always do your own research. "
        "Past performance does not guarantee future results."
    )
    token_usage: dict = Field(default_factory=dict)


class ClaudeRecommender:
    """Generates AI-powered trading recommendations using Claude."""

    def __init__(self) -> None:
        self._client: anthropic.AsyncAnthropic | None = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not settings.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY not configured")
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key
            )
        return self._client

    async def generate(self) -> RecommendationSet:
        """Generate recommendations by calling Claude API."""
        client = self._get_client()
        context = await self._gather_context()
        user_prompt = self._build_user_prompt(context)

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text

        # Check if response was truncated
        if response.stop_reason == "max_tokens":
            logger.warning("Claude response truncated (hit max_tokens)")

        # Parse JSON from response (handle markdown code blocks if present)
        json_text = raw_text
        if "```json" in json_text:
            json_text = json_text.split("```json")[1].split("```")[0]
        elif "```" in json_text:
            json_text = json_text.split("```")[1].split("```")[0]

        try:
            data = json.loads(json_text.strip())
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Claude response: %s", e)
            logger.error("Raw response (first 500 chars): %s", raw_text[:500])
            raise ValueError(f"Invalid JSON in Claude response: {e}") from e

        def _parse_opportunities(opps: list[dict]) -> list[Recommendation]:
            recs = []
            for opp in opps:
                recs.append(Recommendation(
                    symbol=opp["symbol"],
                    market=opp["market"],
                    action=RecommendationAction(opp["action"]),
                    confidence=opp["confidence"],
                    current_price_cents=opp["current_price_cents"],
                    entry_price_cents=opp.get("entry_price_cents"),
                    target_price_cents=opp.get("target_price_cents"),
                    stop_loss_cents=opp.get("stop_loss_cents"),
                    reasoning=opp["reasoning"],
                    risk_notes=opp["risk_notes"],
                    timeframe=opp.get("timeframe", ""),
                ))
            return recs

        crypto_recs = _parse_opportunities(data.get("crypto_opportunities", []))
        asx_recs = _parse_opportunities(data.get("asx_opportunities", []))
        us_recs = _parse_opportunities(data.get("us_opportunities", []))
        uk_recs = _parse_opportunities(data.get("uk_opportunities", []))

        # Also handle legacy "top_opportunities" if present (backward compat)
        all_recs = crypto_recs + asx_recs + us_recs + uk_recs
        if not all_recs:
            all_recs = _parse_opportunities(data.get("top_opportunities", []))

        return RecommendationSet(
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            model_used="claude-sonnet-4-6",
            market_summary=data.get("market_summary", ""),
            top_opportunities=all_recs,
            crypto_opportunities=crypto_recs,
            asx_opportunities=asx_recs,
            us_opportunities=us_recs,
            uk_opportunities=uk_recs,
            market_overview=context.get("market_overview", []),
            symbols_to_avoid=data.get("symbols_to_avoid", []),
            token_usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )

    async def _gather_context(self) -> dict:
        """Build compact market context for Claude prompt."""
        r = aioredis.from_url(settings.redis_url, decode_responses=True, max_connections=5)
        watched = await get_watched_symbols()

        symbols_data = []
        async with async_session() as session:
            for sym in watched:
                df = await self._load_ohlcv(sym["symbol"], sym["timeframe"], lookback_days=30, session=session)
                if df is None or len(df) < 14:
                    symbols_data.append({
                        "symbol": sym["symbol"],
                        "market": sym["market"],
                        "data": "insufficient",
                    })
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                current_price = int(close.iloc[-1])
                prev_close = int(close.iloc[-2]) if len(close) >= 2 else current_price
                pct_change = round(
                    (current_price - prev_close) / prev_close * 100, 2
                ) if prev_close > 0 else 0.0

                # Compute key indicators using existing functions
                rsi_val = float(rsi(close).iloc[-1])
                _, _, macd_hist = macd(close)
                macd_val = float(macd_hist.iloc[-1])
                upper, middle, lower, _ = bollinger_bands(close)
                atr_val = float(atr(high, low, close).iloc[-1])
                adx_val = float(adx(high, low, close).iloc[-1])

                # Bollinger position
                cur_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else 0
                cur_upper = float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else 0
                if pd.isna(rsi_val) or pd.isna(macd_val):
                    bb_pos = "unknown"
                elif current_price < cur_lower:
                    bb_pos = "below_lower"
                elif current_price > cur_upper:
                    bb_pos = "above_upper"
                else:
                    bb_pos = "within"

                # Read cached regime and signal from Redis
                regime = await r.get(f"flashtrade:regime:{sym['symbol']}") or "unknown"
                last_signal = await r.get(f"flashtrade:signal:{sym['symbol']}") or "hold"

                symbols_data.append({
                    "symbol": sym["symbol"],
                    "market": sym["market"],
                    "price_cents": current_price,
                    "change_pct": pct_change,
                    "rsi": round(rsi_val, 1) if not pd.isna(rsi_val) else None,
                    "macd_hist": round(macd_val, 0) if not pd.isna(macd_val) else None,
                    "adx": round(adx_val, 1) if not pd.isna(adx_val) else None,
                    "atr_cents": round(atr_val, 0) if not pd.isna(atr_val) else None,
                    "bb_position": bb_pos,
                    "regime": regime,
                    "last_signal": last_signal,
                })

        await r.aclose()

        return {
            "symbols": symbols_data,
            "market_overview": symbols_data,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }

    def _build_user_prompt(self, context: dict) -> str:
        """Build the user message from market context."""
        lines = [f"Market data as of {context['timestamp_utc']}:\n"]

        lines.append("Symbol Data (price in cents, RSI 0-100, ADX 0-100):")
        lines.append(
            f"{'Symbol':<10} {'Mkt':<7} {'Price':>12} {'24h%':>7} "
            f"{'RSI':>5} {'MACD':>7} {'ADX':>5} {'BB':>12} {'Regime':<10} {'Signal'}"
        )
        lines.append("-" * 95)

        for s in context["symbols"]:
            if s.get("data") == "insufficient":
                lines.append(f"{s['symbol']:<10} {s['market']:<7} insufficient data")
                continue
            rsi_str = f"{s['rsi']}" if s.get("rsi") is not None else "--"
            macd_str = f"{s['macd_hist']}" if s.get("macd_hist") is not None else "--"
            adx_str = f"{s['adx']}" if s.get("adx") is not None else "--"
            lines.append(
                f"{s['symbol']:<10} {s['market']:<7} {s['price_cents']:>12d} "
                f"{s['change_pct']:>+6.1f}% {rsi_str:>5} {macd_str:>7} "
                f"{adx_str:>5} {s['bb_position']:>12} {s['regime']:<10} {s['last_signal']}"
            )

        lines.append("\n3 recs per market (crypto, asx, us, uk). JSON only, no markdown.")
        return "\n".join(lines)

    async def _load_ohlcv(
        self, symbol: str, timeframe: str, lookback_days: int = 30,
        session: AsyncSession | None = None,
    ) -> pd.DataFrame | None:
        """Load OHLCV data from database into a pandas DataFrame."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        async def _query(sess):
            stmt = (
                select(OHLCV).where(
                    OHLCV.symbol == symbol,
                    OHLCV.timeframe == timeframe,
                    OHLCV.timestamp >= cutoff,
                ).order_by(OHLCV.timestamp.asc())
            )
            result = await sess.execute(stmt)
            return result.scalars().all()

        if session is not None:
            rows = await _query(session)
        else:
            async with async_session() as sess:
                rows = await _query(sess)

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


async def cache_recommendations(rec_set: RecommendationSet) -> None:
    """Store recommendations in Redis with 1-hour TTL."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set(REDIS_KEY_RECOMMENDATIONS, rec_set.model_dump_json(), ex=3600)
    await r.delete(REDIS_KEY_RECOMMENDATIONS_ERROR)
    await r.aclose()


async def gather_market_overview() -> list[dict]:
    """Compute market overview (indicators for all watched symbols).

    This is independent of Claude — it just reads OHLCV data and computes
    technical indicators. Results are cached in Redis for 5 minutes.
    """
    r = aioredis.from_url(settings.redis_url, decode_responses=True, max_connections=5)

    # Check cache first
    cached = await r.get(REDIS_KEY_MARKET_OVERVIEW)
    if cached:
        await r.aclose()
        return json.loads(cached)

    watched = await get_watched_symbols(redis_conn=r)
    symbols_data = []

    async with async_session() as session:  # ONE session for all queries
        for sym in watched:
            df = await _load_ohlcv_standalone(sym["symbol"], sym["timeframe"], lookback_days=30, session=session)
            if df is None or len(df) < 14:
                symbols_data.append({
                    "symbol": sym["symbol"],
                    "market": sym["market"],
                    "data": "insufficient",
                })
                continue

            close = df["close"]
            high = df["high"]
            low = df["low"]
            current_price = int(close.iloc[-1])
            prev_close = int(close.iloc[-2]) if len(close) >= 2 else current_price
            pct_change = round(
                (current_price - prev_close) / prev_close * 100, 2
            ) if prev_close > 0 else 0.0

            rsi_val = float(rsi(close).iloc[-1])
            _, _, macd_hist = macd(close)
            macd_val = float(macd_hist.iloc[-1])
            upper, middle, lower, _ = bollinger_bands(close)
            atr_val = float(atr(high, low, close).iloc[-1])
            adx_val = float(adx(high, low, close).iloc[-1])

            cur_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else 0
            cur_upper = float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else 0
            if pd.isna(rsi_val) or pd.isna(macd_val):
                bb_pos = "unknown"
            elif current_price < cur_lower:
                bb_pos = "below_lower"
            elif current_price > cur_upper:
                bb_pos = "above_upper"
            else:
                bb_pos = "within"

            regime = await r.get(f"flashtrade:regime:{sym['symbol']}") or "unknown"
            last_signal = await r.get(f"flashtrade:signal:{sym['symbol']}") or "hold"

            symbols_data.append({
                "symbol": sym["symbol"],
                "market": sym["market"],
                "price_cents": current_price,
                "change_pct": pct_change,
                "rsi": round(rsi_val, 1) if not pd.isna(rsi_val) else None,
                "macd_hist": round(macd_val, 0) if not pd.isna(macd_val) else None,
                "adx": round(adx_val, 1) if not pd.isna(adx_val) else None,
                "atr_cents": round(atr_val, 0) if not pd.isna(atr_val) else None,
                "bb_position": bb_pos,
                "regime": regime,
                "last_signal": last_signal,
            })

    # Cache for 5 minutes
    await r.set(REDIS_KEY_MARKET_OVERVIEW, json.dumps(symbols_data), ex=300)
    await r.aclose()
    return symbols_data


async def _load_ohlcv_standalone(
    symbol: str, timeframe: str, lookback_days: int = 30,
    session: AsyncSession | None = None,
) -> pd.DataFrame | None:
    """Load OHLCV data from database (standalone version for non-class use)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    async def _query(sess):
        stmt = (
            select(OHLCV).where(
                OHLCV.symbol == symbol,
                OHLCV.timeframe == timeframe,
                OHLCV.timestamp >= cutoff,
            ).order_by(OHLCV.timestamp.asc())
        )
        result = await sess.execute(stmt)
        return result.scalars().all()

    if session is not None:
        rows = await _query(session)
    else:
        async with async_session() as sess:
            rows = await _query(sess)

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


REDIS_KEY_MARKET_NEWS = "flashtrade:market_news"

NEWS_SYSTEM_PROMPT = """You are a financial news analyst generating concise market commentary.
Based on current market index levels and conditions, generate 4 market news summaries.

Rules:
- Each summary should be 2-3 sentences of actionable market intelligence
- Focus on what's happening and what it means for traders
- Include specific data points where possible (index levels, percentage moves)
- Write in a professional financial journalist style
- Do NOT provide trading recommendations — just factual market commentary

Respond with ONLY valid JSON (no markdown, no explanation):
{
    "us_news": {"headline": "Short headline (max 10 words)", "summary": "2-3 sentence US market commentary"},
    "global_news": {"headline": "Short headline", "summary": "2-3 sentence global markets commentary"},
    "australian_news": {"headline": "Short headline", "summary": "2-3 sentence Australian market commentary"},
    "notable_news": {"headline": "Short headline", "summary": "2-3 sentence notable market event from any country"}
}"""


class NewsItem(BaseModel):
    headline: str
    summary: str


class MarketNews(BaseModel):
    us_news: NewsItem
    global_news: NewsItem
    australian_news: NewsItem
    notable_news: NewsItem
    generated_at_utc: str
    model_used: str = "claude-haiku-4-5-20251001"
    token_usage: dict = Field(default_factory=dict)


async def generate_market_news() -> MarketNews:
    """Call Claude to generate 4 market news summaries."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build context from index data
    from app.services.data.yfinance_feed import get_indices
    indices = await get_indices()
    context_lines = ["Current market index levels:"]
    for idx in indices:
        context_lines.append(f"  {idx['name']} ({idx['symbol']}): {idx['level']:,.2f} ({idx['change_pct']:+.2f}%)")
    context_lines.append(f"\nTimestamp: {datetime.now(timezone.utc).isoformat()}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=NEWS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n".join(context_lines) + "\n\n4 news summaries as JSON."}],
    )

    raw_text = response.content[0].text
    json_text = raw_text
    if "```json" in json_text:
        json_text = json_text.split("```json")[1].split("```")[0]
    elif "```" in json_text:
        json_text = json_text.split("```")[1].split("```")[0]

    data = json.loads(json_text.strip())

    return MarketNews(
        us_news=NewsItem(**data["us_news"]),
        global_news=NewsItem(**data["global_news"]),
        australian_news=NewsItem(**data["australian_news"]),
        notable_news=NewsItem(**data["notable_news"]),
        generated_at_utc=datetime.now(timezone.utc).isoformat(),
        token_usage={
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    )


async def cache_market_news(news: MarketNews) -> None:
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    await r.set(REDIS_KEY_MARKET_NEWS, news.model_dump_json(), ex=3600)
    await r.aclose()
