"""Recommendation API routes — AI-generated trading recommendations."""

import json
import logging

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends

from app.api.auth import require_api_key
from app.config import settings
from app.services.ai.recommender import (
    REDIS_KEY_RECOMMENDATIONS,
    REDIS_KEY_RECOMMENDATIONS_ERROR,
    REDIS_KEY_MARKET_NEWS,
    gather_market_overview,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


@router.get("/")
async def get_recommendations():
    """Get latest AI trading recommendations from cache.

    Returns cached Claude analysis. No API call is made — the Celery
    beat task generates fresh recommendations hourly.
    """
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        raw = await r.get(REDIS_KEY_RECOMMENDATIONS)
        error = await r.get(REDIS_KEY_RECOMMENDATIONS_ERROR)
        await r.aclose()

        if raw:
            data = json.loads(raw)
            data["cached"] = True
            return data

        # No cache — auto-trigger first generation (with lock to prevent spam)
        if settings.anthropic_api_key and not error:
            r2 = aioredis.from_url(settings.redis_url, decode_responses=True)
            lock = await r2.set("flashtrade:recs:generating", "1", ex=120, nx=True)
            await r2.aclose()
            if lock:
                from app.tasks.recommendation_tasks import generate_recommendations
                generate_recommendations.delay()

        return {
            "generated_at_utc": None,
            "market_summary": "Generating first AI analysis now. Refresh in ~30 seconds." if not error else "Error during analysis. Will retry automatically.",
            "top_opportunities": [],
            "crypto_opportunities": [],
            "asx_opportunities": [],
            "us_opportunities": [],
            "uk_opportunities": [],
            "market_overview": [],
            "symbols_to_avoid": [],
            "disclaimer": (
                "AI-generated analysis for informational purposes only. "
                "Not financial advice. Always do your own research."
            ),
            "cached": False,
            "error": error,
        }
    except Exception as e:
        logger.error("Failed to read recommendations from Redis: %s", e)
        return {
            "error": str(e),
            "top_opportunities": [],
            "market_summary": "Error loading recommendations.",
        }


@router.get("/overview")
async def get_market_overview():
    """Get market overview with technical indicators for all watched symbols.

    Computes RSI, MACD, ADX, ATR, Bollinger position from OHLCV data.
    Cached in Redis for 5 minutes. No Claude API call needed.
    """
    try:
        overview = await gather_market_overview()
        return {"symbols": overview, "count": len(overview)}
    except Exception as e:
        logger.error("Failed to gather market overview: %s", e)
        return {"error": str(e), "symbols": []}


@router.get("/news")
async def get_market_news():
    """Get AI-generated market news summaries.

    If no cached news exists and API key is configured, auto-triggers
    generation so users don't wait for the hourly beat.
    """
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        raw = await r.get(REDIS_KEY_MARKET_NEWS)
        await r.aclose()

        if raw:
            data = json.loads(raw)
            data["cached"] = True
            return data

        # No cache — auto-trigger first generation (with lock to prevent spam)
        if settings.anthropic_api_key:
            r2 = aioredis.from_url(settings.redis_url, decode_responses=True)
            lock = await r2.set("flashtrade:news:generating", "1", ex=120, nx=True)
            await r2.aclose()
            if lock:
                from app.tasks.recommendation_tasks import generate_market_news
                generate_market_news.delay()

        return {
            "us_news": {"headline": "Generating...", "summary": "AI market news is being generated now. Refresh in ~30 seconds."},
            "global_news": {"headline": "Generating...", "summary": "AI market news is being generated now. Refresh in ~30 seconds."},
            "australian_news": {"headline": "Generating...", "summary": "AI market news is being generated now. Refresh in ~30 seconds."},
            "notable_news": {"headline": "Generating...", "summary": "AI market news is being generated now. Refresh in ~30 seconds."},
            "generated_at_utc": None,
            "cached": False,
        }
    except Exception as e:
        logger.error("Failed to read market news from Redis: %s", e)
        return {"error": str(e)}


@router.post("/refresh-news")
async def refresh_news():
    """Trigger immediate news refresh. No API key required."""
    from app.tasks.recommendation_tasks import generate_market_news

    if not settings.anthropic_api_key:
        return {"status": "error", "message": "ANTHROPIC_API_KEY not configured"}

    generate_market_news.delay()
    return {
        "status": "queued",
        "message": "News refresh queued. Results will appear in ~30 seconds.",
    }


@router.post("/refresh", dependencies=[Depends(require_api_key)])
async def refresh_recommendations():
    """Trigger an immediate recommendation refresh.

    Dispatches the Celery task asynchronously. New recommendations
    will appear on the dashboard within ~30 seconds.
    """
    from app.tasks.recommendation_tasks import generate_recommendations

    if not settings.anthropic_api_key:
        return {"status": "error", "message": "ANTHROPIC_API_KEY not configured"}

    generate_recommendations.delay()
    return {
        "status": "queued",
        "message": "Recommendation refresh queued. Results will appear in ~30 seconds.",
    }
