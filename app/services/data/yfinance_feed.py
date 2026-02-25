"""yfinance data feed for ASX (.AX suffix) and US stocks.

Free tier has 15-min delay. No authentication required.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import yfinance as yf

logger = logging.getLogger(__name__)

ASX_SYMBOLS = [
    "BHP.AX", "CBA.AX", "CSL.AX", "WDS.AX", "FMG.AX",
    "NAB.AX", "WBC.AX", "ANZ.AX", "WOW.AX", "RIO.AX",
]
US_SYMBOLS = [
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN",
    "META", "TSLA", "AMD", "NFLX", "QQQ",
]
UK_SYMBOLS = [
    "SHEL.L", "AZN.L", "HSBA.L", "ULVR.L", "BP.L",
    "GSK.L", "RIO.L", "LSEG.L", "REL.L", "DGE.L",
]
CACHE_TTL_SECONDS = 120


@dataclass
class StockPrice:
    """A single stock ticker result."""

    symbol: str
    market: str
    price_cents: int
    currency: str
    change_pct: float
    previous_close_cents: int
    timestamp_utc: str
    delayed: bool


class YFinanceFeed:
    """Pull stock prices from yfinance. Free, no auth, 15-min delayed."""

    def __init__(self) -> None:
        self._cache: dict[str, StockPrice] = {}
        self._cache_time: float = 0.0

    def _is_cache_valid(self) -> bool:
        return (time.monotonic() - self._cache_time) < CACHE_TTL_SECONDS

    def _fetch_all(self) -> list[StockPrice]:
        """Synchronous fetch of all stock tickers."""
        all_symbols = ASX_SYMBOLS + US_SYMBOLS + UK_SYMBOLS
        prices: list[StockPrice] = []

        try:
            tickers = yf.Tickers(" ".join(all_symbols))

            for sym in all_symbols:
                try:
                    ticker = tickers.tickers[sym]
                    info = ticker.fast_info

                    if sym.endswith(".AX"):
                        market, currency = "asx", "AUD"
                    elif sym.endswith(".L"):
                        market, currency = "uk", "GBP"
                    else:
                        market, currency = "us", "USD"

                    last_price = float(info.get("lastPrice", 0) or 0)
                    prev_close = float(info.get("previousClose", 0) or 0)

                    if last_price and prev_close:
                        change_pct = round(((last_price - prev_close) / prev_close) * 100, 2)
                    else:
                        change_pct = 0.0

                    price = StockPrice(
                        symbol=sym,
                        market=market,
                        price_cents=int(round(last_price * 100)),
                        currency=currency,
                        change_pct=change_pct,
                        previous_close_cents=int(round(prev_close * 100)),
                        timestamp_utc=datetime.now(timezone.utc).isoformat(),
                        delayed=True,
                    )
                    prices.append(price)
                    self._cache[sym] = price
                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", sym, e)
                    if sym in self._cache:
                        prices.append(self._cache[sym])

        except Exception as e:
            logger.warning("yfinance batch fetch failed: %s", e)
            return list(self._cache.values())

        if prices:
            self._cache_time = time.monotonic()

        return prices

    async def get_prices(self) -> list[StockPrice]:
        """Fetch current stock prices. Returns cached data if fresh."""
        if self._is_cache_valid() and self._cache:
            return list(self._cache.values())

        return await asyncio.to_thread(self._fetch_all)


INDEX_SYMBOLS = {
    "^AXJO": {"name": "ASX 200", "market": "asx"},
    "^FTSE": {"name": "FTSE 100", "market": "uk"},
    "^IXIC": {"name": "NASDAQ", "market": "us"},
    "^GSPC": {"name": "S&P 500", "market": "us"},
}


def fetch_indices_sync() -> list[dict]:
    """Fetch major market index levels."""
    results = []
    try:
        tickers = yf.Tickers(" ".join(INDEX_SYMBOLS.keys()))
        for sym, meta in INDEX_SYMBOLS.items():
            try:
                ticker = tickers.tickers[sym]
                info = ticker.fast_info
                last_price = float(info.get("lastPrice", 0) or 0)
                prev_close = float(info.get("previousClose", 0) or 0)
                change_pct = round(((last_price - prev_close) / prev_close) * 100, 2) if prev_close else 0.0
                results.append({
                    "symbol": sym,
                    "name": meta["name"],
                    "market": meta["market"],
                    "level": round(last_price, 2),
                    "change_pct": change_pct,
                    "previous_close": round(prev_close, 2),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.warning("Failed to fetch index %s: %s", sym, e)
    except Exception as e:
        logger.warning("Index batch fetch failed: %s", e)
    return results


async def get_indices() -> list[dict]:
    """Async wrapper for index fetch with cache."""
    return await asyncio.to_thread(fetch_indices_sync)
