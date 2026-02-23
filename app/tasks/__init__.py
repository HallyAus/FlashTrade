"""Celery app and task registration."""

from celery import Celery

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
        # Schedules added as data feeds are implemented
    },
)

# Import tasks so Celery discovers them
from app.tasks import data_tasks, trade_tasks, monitoring_tasks  # noqa: F401, E402
