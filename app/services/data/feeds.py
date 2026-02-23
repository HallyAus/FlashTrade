"""Shared feed singletons — reuse across all API endpoints for cache efficiency.

Both CCXTFeed and YFinanceFeed have in-memory caches (60s and 120s TTL).
Creating new instances per request bypasses the cache. This module provides
process-wide singletons so all endpoints share the same cached data.
"""

from app.services.data.ccxt_feed import CCXTFeed
from app.services.data.yfinance_feed import YFinanceFeed

ccxt_feed = CCXTFeed()
yfinance_feed = YFinanceFeed()


async def get_live_prices() -> dict[str, int]:
    """Fetch live prices from all feeds, returning {symbol: price_cents}.

    Uses the shared singletons so cache is hit when possible.
    """
    prices: dict[str, int] = {}
    try:
        for p in await ccxt_feed.get_prices():
            prices[p.symbol] = p.price_cents
    except Exception:
        pass
    try:
        for p in await yfinance_feed.get_prices():
            prices[p.symbol] = p.price_cents
    except Exception:
        pass
    return prices
