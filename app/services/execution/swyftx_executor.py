"""Swyftx order executor for Australian crypto.

Custom REST client — CCXT does not support Swyftx for authenticated trading.
Auth: API key → POST /auth/refresh/ → JWT Bearer token (24h TTL).
Demo mode available at api.demo.swyftx.com.au ($10K mock AUD).
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.database import async_session
from app.models.trade import Trade
from app.models.journal import JournalEntry
from app.services.risk_manager import Order, RiskManager, RiskVerdict

logger = logging.getLogger(__name__)

BASE_URL_LIVE = "https://api.swyftx.com.au"
BASE_URL_DEMO = "https://api.demo.swyftx.com.au"

# Swyftx order type codes
ORDER_MARKET_BUY = 1
ORDER_MARKET_SELL = 2
ORDER_LIMIT_BUY = 3
ORDER_LIMIT_SELL = 4
ORDER_STOP_LIMIT_BUY = 5
ORDER_STOP_LIMIT_SELL = 6


class SwyftxClient:
    """Low-level HTTP client for the Swyftx REST API."""

    def __init__(self, api_key: str, demo: bool = True) -> None:
        self._api_key = api_key
        base_url = BASE_URL_DEMO if demo else BASE_URL_LIVE
        self._client = httpx.AsyncClient(base_url=base_url, timeout=15.0)
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    async def _ensure_auth(self) -> None:
        """Refresh JWT if expired or missing."""
        if self._access_token and self._token_expiry and datetime.now(timezone.utc) < self._token_expiry:
            return

        resp = await self._client.post(
            "/auth/refresh/",
            json={"apiKey": self._api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["accessToken"]
        self._token_expiry = datetime.now(timezone.utc) + timedelta(hours=23)
        logger.info("Swyftx: JWT refreshed, expires %s", self._token_expiry.isoformat())

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def get_balances(self) -> list[dict]:
        """Get all account balances."""
        await self._ensure_auth()
        resp = await self._client.get("/user/balance/", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def place_order(
        self,
        primary: str,
        secondary: str,
        quantity: float,
        asset_quantity: str,
        order_type: int,
        trigger: float = 0,
    ) -> int:
        """Place an order. Returns order ID."""
        await self._ensure_auth()
        body = {
            "primary": primary,
            "secondary": secondary,
            "quantity": quantity,
            "assetQuantity": asset_quantity,
            "orderType": order_type,
            "trigger": trigger,
        }
        resp = await self._client.post("/orders/", json=body, headers=self._headers())
        resp.raise_for_status()
        order_id = resp.json().get("orderId")
        logger.info("Swyftx order placed: %s (id=%s)", body, order_id)
        return order_id

    async def cancel_order(self, order_id: int) -> None:
        """Cancel an open order."""
        await self._ensure_auth()
        resp = await self._client.delete(f"/orders/{order_id}/", headers=self._headers())
        resp.raise_for_status()

    async def get_orders(self, asset: str) -> list[dict]:
        """Get orders for an asset."""
        await self._ensure_auth()
        resp = await self._client.get(f"/orders/{asset}/", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def get_exchange_rate(self, buy: str, sell: str, amount: float) -> dict:
        """Get exchange rate quote."""
        await self._ensure_auth()
        resp = await self._client.post(
            "/orders/rate/",
            json={"buy": buy, "sell": sell, "amount": amount},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()


class SwyftxExecutor:
    """Execute crypto orders via Swyftx API.

    All orders go through RiskManager before execution.
    Supports demo mode (paper trading with real Swyftx demo account).
    """

    def __init__(self, risk_manager: RiskManager, demo: bool = True) -> None:
        self._risk_manager = risk_manager
        self._demo = demo
        api_key = settings.swyftx_api_key
        if not api_key:
            raise ValueError("SWYFTX_API_KEY not configured")
        self._client = SwyftxClient(api_key, demo=demo)

    async def submit_order(self, order: Order) -> dict:
        """Submit a crypto order through risk checks and execute via Swyftx."""
        # Step 1: Risk check
        verdict: RiskVerdict = self._risk_manager.evaluate(order)
        if not verdict.approved:
            logger.warning("Swyftx order rejected: %s", verdict.reason)
            await self._record_trade(order, "rejected", reason=verdict.reason)
            return {"status": "rejected", "reason": verdict.reason}

        # Step 2: Get exchange rate for accurate pricing
        try:
            if order.side == "buy":
                rate = await self._client.get_exchange_rate(
                    buy=order.symbol, sell="AUD", amount=order.quantity_cents / 100
                )
            else:
                rate = await self._client.get_exchange_rate(
                    buy="AUD", sell=order.symbol, amount=order.quantity_cents / 100
                )
        except Exception as e:
            logger.error("Failed to get exchange rate: %s", e)
            await self._record_trade(order, "failed", reason=f"Rate check failed: {e}")
            return {"status": "failed", "reason": str(e)}

        # Step 3: Place order
        try:
            order_type = ORDER_MARKET_BUY if order.side == "buy" else ORDER_MARKET_SELL
            quantity_aud = order.quantity_cents / 100

            order_id = await self._client.place_order(
                primary=order.symbol,
                secondary="AUD",
                quantity=quantity_aud,
                asset_quantity=order.symbol,
                order_type=order_type,
            )

            trade_id = await self._record_trade(
                order, "filled",
                broker_order_id=str(order_id),
                reason=order.reason,
            )

            logger.info(
                "Swyftx %s order filled: %s %s AUD (order_id=%s, demo=%s)",
                order.side, order.symbol, quantity_aud, order_id, self._demo,
            )

            return {
                "status": "filled",
                "trade_id": trade_id,
                "broker_order_id": order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity_aud": quantity_aud,
                "demo": self._demo,
            }

        except Exception as e:
            logger.error("Swyftx order failed: %s", e)
            await self._record_trade(order, "failed", reason=f"Execution failed: {e}")
            return {"status": "failed", "reason": str(e)}

    async def get_balances(self) -> list[dict]:
        """Get Swyftx account balances."""
        return await self._client.get_balances()

    async def get_orders(self, asset: str) -> list[dict]:
        """Get open orders for an asset."""
        return await self._client.get_orders(asset)

    async def cancel_order(self, order_id: int) -> None:
        """Cancel an open order."""
        await self._client.cancel_order(order_id)

    async def _record_trade(
        self,
        order: Order,
        status: str,
        broker_order_id: str | None = None,
        reason: str | None = None,
    ) -> int | None:
        """Record a trade in the database."""
        async with async_session() as session:
            now = datetime.now(timezone.utc)
            trade = Trade(
                symbol=order.symbol,
                market="crypto",
                side=order.side,
                order_type=order.order_type,
                quantity_cents=order.quantity_cents,
                price_cents=order.price_cents,
                stop_loss_cents=order.stop_loss_cents,
                status=status,
                strategy=order.strategy,
                broker_order_id=broker_order_id,
                reason=reason,
                created_at=now,
                filled_at=now if status == "filled" else None,
            )
            session.add(trade)
            await session.flush()

            journal = JournalEntry(
                trade_id=trade.id,
                symbol=order.symbol,
                action=f"swyftx_{order.side}_{status}",
                strategy=order.strategy,
                reasoning=reason or order.reason,
                created_at=now,
            )
            session.add(journal)
            await session.commit()
            return trade.id
