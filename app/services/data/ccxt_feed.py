"""CCXT data feed for crypto prices.

Uses CCXT public endpoints — no authentication required for market data.
Swyftx Auth0 OAuth 2.0 is only needed for trading (Day 5+).
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import ccxt

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60

SWYFTX_SYMBOLS = ["BTC/AUD", "ETH/AUD", "SOL/AUD"]
BINANCE_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


@dataclass
class CryptoPrice:
    """A single crypto ticker result."""

    symbol: str
    price_cents: int
    currency: str
    change_24h_pct: float
    volume_24h: float
    bid_cents: int
    ask_cents: int
    timestamp_utc: str


class CCXTFeed:
    """Pull crypto ticker data via CCXT public API.

    Tries Swyftx first (AUD pairs), falls back to Binance (USDT pairs).
    Uses an in-memory cache with TTL to avoid hitting rate limits.
    """

    def __init__(self) -> None:
        self._exchange: ccxt.Exchange | None = None
        self._symbols: list[str] = []
        self._currency: str = "AUD"
        self._cache: dict[str, CryptoPrice] = {}
        self._cache_time: float = 0.0
        self._initialized: bool = False

    def _init_exchange(self) -> None:
        """Lazy init — try Swyftx, fall back to Binance."""
        if self._initialized:
            return

        try:
            exchange = ccxt.swyftx({"enableRateLimit": True, "timeout": 10000})
            exchange.load_markets()
            self._exchange = exchange
            self._symbols = SWYFTX_SYMBOLS
            self._currency = "AUD"
            logger.info("CCXT: Using Swyftx (AUD pairs)")
        except Exception as e:
            logger.warning("Swyftx unavailable (%s), falling back to Binance", e)
            try:
                exchange = ccxt.binance({"enableRateLimit": True, "timeout": 10000})
                exchange.load_markets()
                self._exchange = exchange
                self._symbols = BINANCE_SYMBOLS
                self._currency = "USDT"
                logger.info("CCXT: Using Binance (USDT pairs)")
            except Exception as e2:
                logger.error("Both Swyftx and Binance failed: %s", e2)

        self._initialized = True

    def _is_cache_valid(self) -> bool:
        return (time.monotonic() - self._cache_time) < CACHE_TTL_SECONDS

    def _fetch_all(self) -> list[CryptoPrice]:
        """Synchronous fetch of all crypto tickers."""
        self._init_exchange()

        if not self._exchange:
            return list(self._cache.values())

        prices: list[CryptoPrice] = []
        for symbol in self._symbols:
            short = symbol.split("/")[0]
            try:
                ticker = self._exchange.fetch_ticker(symbol)
                last = ticker.get("last") or 0
                price = CryptoPrice(
                    symbol=short,
                    price_cents=int(round(last * 100)),
                    currency=self._currency,
                    change_24h_pct=round(ticker.get("percentage") or 0.0, 2),
                    volume_24h=round(ticker.get("baseVolume") or 0.0, 2),
                    bid_cents=int(round((ticker.get("bid") or last) * 100)),
                    ask_cents=int(round((ticker.get("ask") or last) * 100)),
                    timestamp_utc=ticker.get("datetime") or "",
                )
                prices.append(price)
                self._cache[short] = price
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", symbol, e)
                if short in self._cache:
                    prices.append(self._cache[short])

        if prices:
            self._cache_time = time.monotonic()

        return prices

    async def get_prices(self) -> list[CryptoPrice]:
        """Fetch current crypto prices. Returns cached data if fresh."""
        if self._is_cache_valid() and self._cache:
            return list(self._cache.values())

        # ccxt is synchronous — run in thread to avoid blocking the event loop
        return await asyncio.to_thread(self._fetch_all)
