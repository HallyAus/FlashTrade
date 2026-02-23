"""Trading execution tasks — strategy evaluation and order placement.

TODO: Implement in Day 5+.
"""

from app.tasks import celery_app


@celery_app.task
def evaluate_signals() -> None:
    """Run strategies against latest data and generate signals."""
    pass


@celery_app.task
def execute_approved_orders() -> None:
    """Execute orders that passed risk manager evaluation."""
    pass
