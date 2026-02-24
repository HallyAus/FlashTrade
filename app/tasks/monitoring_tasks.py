"""Monitoring tasks — health checks, P&L tracking, alerts.

Celery tasks for daily reporting and system health monitoring.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task
def daily_pnl_report() -> dict:
    """Generate and send daily P&L report via webhook."""
    return _run_async(_daily_pnl_report_async())


async def _daily_pnl_report_async() -> dict:
    from sqlalchemy import select

    from app.database import async_session
    from app.models.trade import Trade
    from app.models.position import Position
    from app.services.alerting import AlertService

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    async with async_session() as session:
        # Fetch trades from last 24h
        result = await session.execute(
            select(Trade).where(
                Trade.created_at >= cutoff,
                Trade.status == "filled",
            )
        )
        trades = result.scalars().all()

        # Count open positions
        pos_result = await session.execute(select(Position))
        positions = pos_result.scalars().all()

    # Sum P&L from sell trades (buys don't have realized P&L)
    total_pnl_cents = 0
    buy_count = 0
    sell_count = 0
    for t in trades:
        if t.side == "buy":
            buy_count += 1
        elif t.side == "sell":
            sell_count += 1
            # Extract P&L from reason string if available, otherwise estimate
            if t.reason and "P&L:" in t.reason:
                try:
                    pnl_str = t.reason.split("P&L:")[1].strip().split()[0]
                    total_pnl_cents += int(pnl_str)
                except (ValueError, IndexError):
                    pass

    alert_service = AlertService()
    await alert_service.daily_summary(
        total_trades=len(trades),
        pnl_cents=total_pnl_cents,
        open_positions=len(positions),
        portfolio_value_cents=1_000_000,  # TODO: calculate from positions + cash
    )

    summary = {
        "period": "24h",
        "total_trades": len(trades),
        "buys": buy_count,
        "sells": sell_count,
        "pnl_cents": total_pnl_cents,
        "open_positions": len(positions),
    }
    logger.info("Daily P&L report: %s", summary)
    return summary


@celery_app.task
def health_check() -> dict:
    """Check database, Redis, and data freshness. Alert if unhealthy."""
    return _run_async(_health_check_async())


async def _health_check_async() -> dict:
    from datetime import datetime, timedelta, timezone

    import redis.asyncio as aioredis
    from sqlalchemy import text, select, func

    from app.config import settings
    from app.database import async_session
    from app.models.ohlcv import OHLCV
    from app.services.alerting import AlertService

    alert_service = AlertService()
    checks: dict[str, dict] = {}

    # 1. Database connectivity
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "error", "error": str(e)}
        await alert_service.system_error("Database", f"Connection failed: {e}")

    # 2. Redis connectivity
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.close()
        checks["redis"] = {"status": "ok"}
    except Exception as e:
        checks["redis"] = {"status": "error", "error": str(e)}
        await alert_service.system_error("Redis", f"Connection failed: {e}")

    # 3. Data freshness — check if we have recent OHLCV data
    try:
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=2)
        async with async_session() as session:
            result = await session.execute(
                select(func.max(OHLCV.timestamp)).where(
                    OHLCV.timeframe == "1h"
                )
            )
            latest = result.scalar_one_or_none()

        if latest is None:
            checks["data_freshness"] = {"status": "warning", "message": "No OHLCV data found"}
        elif latest < stale_threshold:
            checks["data_freshness"] = {
                "status": "stale",
                "latest": latest.isoformat(),
                "threshold": stale_threshold.isoformat(),
            }
            await alert_service.system_error(
                "Data Feed",
                f"Latest 1h OHLCV data is stale: {latest.isoformat()} "
                f"(threshold: {stale_threshold.isoformat()})",
            )
        else:
            checks["data_freshness"] = {"status": "ok", "latest": latest.isoformat()}
    except Exception as e:
        checks["data_freshness"] = {"status": "error", "error": str(e)}

    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    logger.info("Health check: %s — %s", overall, checks)
    return {"status": overall, "checks": checks}
