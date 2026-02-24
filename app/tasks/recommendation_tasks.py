"""AI recommendation generation task.

Calls Claude API hourly to analyze market data and generate trading recommendations.
Results cached in Redis for instant dashboard access.
"""

import asyncio
import logging

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=120)
def generate_recommendations(self) -> dict:
    """Generate AI trading recommendations via Claude API.

    Runs hourly via Celery beat. Gathers market data, calls Claude,
    caches results in Redis. Retries up to 2 times with 120s delay.
    """
    return _run_async(_generate_async(self))


async def _generate_async(task) -> dict:
    import redis.asyncio as aioredis

    from app.config import settings
    from app.services.ai.recommender import (
        ClaudeRecommender,
        REDIS_KEY_RECOMMENDATIONS_ERROR,
        cache_recommendations,
    )

    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY not configured, skipping recommendations")
        return {"status": "skipped", "reason": "no_api_key"}

    recommender = ClaudeRecommender()

    try:
        rec_set = await recommender.generate()
        await cache_recommendations(rec_set)

        logger.info(
            "Recommendations generated: %d opportunities, tokens: %s",
            len(rec_set.top_opportunities),
            rec_set.token_usage,
        )
        return {
            "status": "completed",
            "opportunities": len(rec_set.top_opportunities),
            "tokens": rec_set.token_usage,
        }

    except Exception as e:
        logger.error("Recommendation generation failed: %s", e)
        # Store error in Redis so dashboard can show it
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            await r.set(REDIS_KEY_RECOMMENDATIONS_ERROR, str(e), ex=3600)
            await r.aclose()
        except Exception:
            pass
        raise task.retry(exc=e)
