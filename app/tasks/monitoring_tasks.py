"""Monitoring tasks — health checks, P&L tracking, alerts.

TODO: Implement in Day 11.
"""

from app.tasks import celery_app


@celery_app.task
def daily_pnl_report() -> None:
    """Generate and send daily P&L report."""
    pass


@celery_app.task
def health_check() -> None:
    """Check data feed health, broker connectivity, container status."""
    pass
