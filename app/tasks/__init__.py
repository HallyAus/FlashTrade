"""Celery app and task registration."""

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "flashtrade",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        # --- Data pulls ---
        # Crypto: every 1 minute, 24/7
        "pull-crypto-1m": {
            "task": "app.tasks.data_tasks.pull_crypto_data",
            "schedule": 60.0,
        },
        # US stocks: every 15 min, Mon-Fri 14:00-21:30 UTC (covers market hours)
        "pull-us-stocks-15m": {
            "task": "app.tasks.data_tasks.pull_us_stock_data",
            "schedule": crontab(minute="*/15", hour="14-21", day_of_week="1-5"),
        },
        # ASX stocks: every 15 min, Mon-Fri 23:00-07:00 UTC (covers AEST/AEDT hours)
        "pull-asx-stocks-15m": {
            "task": "app.tasks.data_tasks.pull_asx_data",
            "schedule": crontab(minute="*/15", hour="23,0-7", day_of_week="0-4"),
        },
        # UK stocks: every 15 min, Mon-Fri 07:00-17:00 UTC (covers LSE hours)
        "pull-uk-stocks-15m": {
            "task": "app.tasks.data_tasks.pull_uk_data",
            "schedule": crontab(minute="*/15", hour="7-17", day_of_week="1-5"),
        },
        # --- Trading ---
        # Evaluate signals every 5 minutes (strategies check market hours internally)
        "evaluate-signals-5m": {
            "task": "app.tasks.trade_tasks.evaluate_signals",
            "schedule": 300.0,
        },
        # Check stop-losses every 60 seconds
        "check-stops-1m": {
            "task": "app.tasks.trade_tasks.check_stop_losses",
            "schedule": 60.0,
        },
        # --- AI Recommendations ---
        # Claude analysis: every hour
        "generate-recommendations-1h": {
            "task": "app.tasks.recommendation_tasks.generate_recommendations",
            "schedule": 3600.0,
        },
        # Market news: every hour at :30
        "generate-market-news-1h": {
            "task": "app.tasks.recommendation_tasks.generate_market_news",
            "schedule": crontab(minute=30),
        },
    },
)

# Import tasks so Celery discovers them
from app.tasks import data_tasks, trade_tasks, monitoring_tasks, recommendation_tasks  # noqa: F401, E402
