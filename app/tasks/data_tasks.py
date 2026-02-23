"""Scheduled data ingestion tasks.

TODO: Implement in Day 2.
"""

from app.tasks import celery_app


@celery_app.task
def pull_crypto_data() -> None:
    """Pull crypto OHLCV every 1 minute via CCXT."""
    pass


@celery_app.task
def pull_us_stock_data() -> None:
    """Pull US stock OHLCV every 15 minutes during market hours."""
    pass


@celery_app.task
def pull_asx_data() -> None:
    """Pull ASX OHLCV every 15 minutes during market hours."""
    pass
