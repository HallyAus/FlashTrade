"""Alerting service — webhook notifications for trade events, errors, and monitoring.

Sends alerts to a Discord/Slack-compatible webhook URL. Falls back to logging
when no webhook is configured.
"""

import logging
from datetime import datetime, timezone
from enum import Enum

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Emoji mapping for alert levels (Discord/Slack rendering)
_LEVEL_EMOJI = {
    AlertLevel.INFO: "\u2139\ufe0f",
    AlertLevel.WARNING: "\u26a0\ufe0f",
    AlertLevel.ERROR: "\u274c",
    AlertLevel.CRITICAL: "\U0001f6a8",
}


class AlertService:
    """Send webhook alerts for trading events.

    Compatible with Discord and Slack incoming webhooks.
    Falls back to logging when no webhook URL is configured.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url or settings.alert_webhook_url

    async def send(self, title: str, message: str, level: AlertLevel = AlertLevel.INFO) -> bool:
        """Send an alert via webhook.

        Args:
            title: Short alert title.
            message: Alert body text.
            level: Severity level.

        Returns:
            True if sent successfully, False otherwise.
        """
        emoji = _LEVEL_EMOJI.get(level, "")
        formatted = f"**{emoji} {title}**\n{message}"

        if not self._webhook_url:
            # No webhook configured — fall back to logging
            log_fn = {
                AlertLevel.INFO: logger.info,
                AlertLevel.WARNING: logger.warning,
                AlertLevel.ERROR: logger.error,
                AlertLevel.CRITICAL: logger.critical,
            }.get(level, logger.info)
            log_fn("ALERT [%s]: %s — %s", level.value, title, message)
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Discord webhook format (also works with Slack)
                payload = {"content": formatted}
                resp = await client.post(self._webhook_url, json=payload)
                if resp.status_code in (200, 204):
                    return True
                logger.warning(
                    "Webhook returned %d: %s", resp.status_code, resp.text[:200]
                )
                return False
        except Exception as e:
            logger.error("Failed to send webhook alert: %s", e)
            return False

    async def trade_fill(
        self,
        symbol: str,
        side: str,
        price_cents: int,
        quantity_cents: int,
        strategy: str,
    ) -> bool:
        """Alert on trade fill."""
        return await self.send(
            title=f"Trade Filled: {side.upper()} {symbol}",
            message=(
                f"Price: ${price_cents / 100:.2f}\n"
                f"Size: ${quantity_cents / 100:.2f}\n"
                f"Strategy: {strategy}"
            ),
            level=AlertLevel.INFO,
        )

    async def stop_loss_hit(
        self,
        symbol: str,
        price_cents: int,
        stop_cents: int,
        pnl_cents: int,
    ) -> bool:
        """Alert on stop-loss trigger."""
        return await self.send(
            title=f"Stop-Loss Hit: {symbol}",
            message=(
                f"Triggered at ${price_cents / 100:.2f} (stop: ${stop_cents / 100:.2f})\n"
                f"P&L: ${pnl_cents / 100:+.2f}"
            ),
            level=AlertLevel.WARNING,
        )

    async def circuit_breaker(self, consecutive_losses: int, pause_minutes: int) -> bool:
        """Alert on circuit breaker activation."""
        return await self.send(
            title="Circuit Breaker Activated",
            message=(
                f"Trading paused for {pause_minutes} minutes.\n"
                f"Consecutive losses: {consecutive_losses}"
            ),
            level=AlertLevel.ERROR,
        )

    async def daily_summary(
        self,
        total_trades: int,
        pnl_cents: int,
        open_positions: int,
        portfolio_value_cents: int,
    ) -> bool:
        """Send daily P&L summary."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return await self.send(
            title=f"Daily Summary — {now}",
            message=(
                f"Trades today: {total_trades}\n"
                f"P&L: ${pnl_cents / 100:+.2f}\n"
                f"Open positions: {open_positions}\n"
                f"Portfolio value: ${portfolio_value_cents / 100:,.2f}"
            ),
            level=AlertLevel.INFO,
        )

    async def system_error(self, component: str, error: str) -> bool:
        """Alert on system-level error (DB down, feed failure, etc.)."""
        return await self.send(
            title=f"System Error: {component}",
            message=error,
            level=AlertLevel.CRITICAL,
        )
