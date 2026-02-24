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

        return {
            "generated_at_utc": None,
            "market_summary": "No recommendations available yet. Waiting for first hourly analysis.",
            "top_opportunities": [],
            "crypto_opportunities": [],
            "asx_opportunities": [],
            "us_opportunities": [],
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
