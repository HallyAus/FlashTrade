"""Scheduled data ingestion tasks.

These run on Celery Beat to keep the OHLCV table current.
Crypto: every 1 minute. Stocks: every 15 minutes during market hours.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from synchronous Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def pull_crypto_data(self) -> dict:
    """Pull crypto OHLCV every 1 minute via CCXT and persist to DB."""
    from app.services.data.ingestion import ingest_crypto_ohlcv

    try:
        count = _run_async(ingest_crypto_ohlcv(timeframe="1h", limit=5))
        logger.info("pull_crypto_data: ingested %d rows", count)
        return {"status": "ok", "rows": count}
    except Exception as e:
        logger.error("pull_crypto_data failed: %s", e)
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def pull_us_stock_data(self) -> dict:
    """Pull US stock OHLCV every 15 minutes during market hours."""
    from app.services.data.ingestion import ingest_stock_ohlcv

    # US market hours: 9:30-16:00 ET (14:30-21:00 UTC)
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if not (14 <= hour <= 21):
        logger.debug("US market closed (UTC hour=%d), skipping", hour)
        return {"status": "skipped", "reason": "market_closed"}

    try:
        count = _run_async(ingest_stock_ohlcv("us", timeframe="1d", period="5d"))
        logger.info("pull_us_stock_data: ingested %d rows", count)
        return {"status": "ok", "rows": count}
    except Exception as e:
        logger.error("pull_us_stock_data failed: %s", e)
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def pull_asx_data(self) -> dict:
    """Pull ASX OHLCV every 15 minutes during market hours."""
    from app.services.data.ingestion import ingest_stock_ohlcv

    # ASX market hours: 10:00-16:00 AEST (00:00-06:00 UTC during AEST,
    # or 23:00-05:00 UTC during AEDT). Use a broad window.
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if not (hour <= 7 or hour >= 23):
        logger.debug("ASX market closed (UTC hour=%d), skipping", hour)
        return {"status": "skipped", "reason": "market_closed"}

    try:
        count = _run_async(ingest_stock_ohlcv("asx", timeframe="1d", period="5d"))
        logger.info("pull_asx_data: ingested %d rows", count)
        return {"status": "ok", "rows": count}
    except Exception as e:
        logger.error("pull_asx_data failed: %s", e)
        raise self.retry(exc=e)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=60)
def pull_uk_data(self) -> dict:
    """Pull UK stock OHLCV every 15 minutes during UK market hours."""
    return _run_async(_pull_uk_async(self))


async def _pull_uk_async(task) -> dict:
    from app.services.data.ingestion import ingest_stock_ohlcv

    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if not (7 <= hour <= 17):
        logger.debug("UK market closed (UTC hour=%d), skipping", hour)
        return {"status": "skipped", "reason": "market_closed"}

    try:
        count = await ingest_stock_ohlcv("uk", timeframe="1d", period="5d")
        logger.info("pull_uk_data: ingested %d rows", count)
        return {"status": "ok", "rows": count}
    except Exception as e:
        logger.error("pull_uk_data failed: %s", e)
        raise task.retry(exc=e)
