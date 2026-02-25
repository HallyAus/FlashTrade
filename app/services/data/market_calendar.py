"""Market calendar — trading hours, holidays, and session awareness.

Handles ASX (AEST/AEDT), US (Eastern), and Crypto (24/7).
All internal logic in UTC; display conversion happens in the API layer.
"""

import logging
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import NamedTuple

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Timezone definitions
TZ_UTC = timezone.utc
TZ_AEST = ZoneInfo("Australia/Sydney")  # Handles AEST/AEDT automatically
TZ_US_EASTERN = ZoneInfo("US/Eastern")  # Handles EST/EDT automatically
TZ_UK = ZoneInfo("Europe/London")  # Handles GMT/BST automatically


class Market(str, Enum):
    ASX = "asx"
    US = "us"
    CRYPTO = "crypto"
    UK = "uk"


# Known market holidays (date only, no time). Add more as needed.
# These are approximate — doesn't cover every half-day or special close.
US_HOLIDAYS_2026 = {
    datetime(2026, 1, 1).date(),   # New Year's Day
    datetime(2026, 1, 19).date(),  # MLK Day
    datetime(2026, 2, 16).date(),  # Presidents' Day
    datetime(2026, 4, 3).date(),   # Good Friday
    datetime(2026, 5, 25).date(),  # Memorial Day
    datetime(2026, 6, 19).date(),  # Juneteenth
    datetime(2026, 7, 3).date(),   # Independence Day (observed)
    datetime(2026, 9, 7).date(),   # Labor Day
    datetime(2026, 11, 26).date(), # Thanksgiving
    datetime(2026, 12, 25).date(), # Christmas
}

ASX_HOLIDAYS_2026 = {
    datetime(2026, 1, 1).date(),   # New Year's Day
    datetime(2026, 1, 26).date(),  # Australia Day
    datetime(2026, 4, 3).date(),   # Good Friday
    datetime(2026, 4, 6).date(),   # Easter Monday
    datetime(2026, 4, 27).date(),  # ANZAC Day (observed)
    datetime(2026, 6, 8).date(),   # Queen's Birthday
    datetime(2026, 12, 25).date(), # Christmas
    datetime(2026, 12, 28).date(), # Boxing Day (observed)
}

UK_HOLIDAYS_2026 = {
    datetime(2026, 1, 1).date(),   # New Year's Day
    datetime(2026, 4, 3).date(),   # Good Friday
    datetime(2026, 4, 6).date(),   # Easter Monday
    datetime(2026, 5, 4).date(),   # Early May Bank Holiday
    datetime(2026, 5, 25).date(),  # Spring Bank Holiday
    datetime(2026, 8, 31).date(),  # Summer Bank Holiday
    datetime(2026, 12, 25).date(), # Christmas
    datetime(2026, 12, 28).date(), # Boxing Day (observed)
}

MARKET_HOLIDAYS = {
    Market.US: US_HOLIDAYS_2026,
    Market.ASX: ASX_HOLIDAYS_2026,
    Market.CRYPTO: set(),
    Market.UK: UK_HOLIDAYS_2026,
}


class MarketSession(NamedTuple):
    """A trading session with open/close times in local timezone."""

    market: Market
    open_time: time  # Local time
    close_time: time  # Local time
    tz: ZoneInfo
    trading_days: tuple[int, ...]  # 0=Monday, 6=Sunday


# Market session definitions (local times)
SESSIONS = {
    Market.ASX: MarketSession(
        market=Market.ASX,
        open_time=time(10, 0),   # 10:00 AEST/AEDT
        close_time=time(16, 0),  # 16:00 AEST/AEDT
        tz=TZ_AEST,
        trading_days=(0, 1, 2, 3, 4),  # Mon-Fri
    ),
    Market.US: MarketSession(
        market=Market.US,
        open_time=time(9, 30),   # 09:30 ET
        close_time=time(16, 0),  # 16:00 ET
        tz=TZ_US_EASTERN,
        trading_days=(0, 1, 2, 3, 4),  # Mon-Fri
    ),
    Market.CRYPTO: MarketSession(
        market=Market.CRYPTO,
        open_time=time(0, 0),
        close_time=time(23, 59, 59),
        tz=TZ_UTC,
        trading_days=(0, 1, 2, 3, 4, 5, 6),  # Every day
    ),
    Market.UK: MarketSession(
        market=Market.UK,
        open_time=time(8, 0),    # 08:00 GMT/BST
        close_time=time(16, 30), # 16:30 GMT/BST
        tz=TZ_UK,
        trading_days=(0, 1, 2, 3, 4),  # Mon-Fri
    ),
}


def is_market_open(market: Market, at_utc: datetime | None = None) -> bool:
    """Check if a market is currently open.

    Args:
        market: Which market to check.
        at_utc: UTC datetime to check (defaults to now).

    Returns:
        True if the market is open at the given time.
    """
    if market == Market.CRYPTO:
        return True  # 24/7

    session = SESSIONS[market]
    if at_utc is None:
        at_utc = datetime.now(TZ_UTC)

    # Convert UTC to the market's local timezone
    local_dt = at_utc.astimezone(session.tz)

    # Check if it's a trading day
    if local_dt.weekday() not in session.trading_days:
        return False

    # Check holidays
    holidays = MARKET_HOLIDAYS.get(market, set())
    if local_dt.date() in holidays:
        return False

    # Check if within trading hours
    local_time = local_dt.time()
    return session.open_time <= local_time < session.close_time


def next_open(market: Market, after_utc: datetime | None = None) -> datetime:
    """Get the next market open time in UTC.

    Args:
        market: Which market.
        after_utc: Find next open after this time (defaults to now).

    Returns:
        Next open time as UTC datetime.
    """
    if market == Market.CRYPTO:
        return after_utc or datetime.now(TZ_UTC)

    session = SESSIONS[market]
    if after_utc is None:
        after_utc = datetime.now(TZ_UTC)

    local_dt = after_utc.astimezone(session.tz)

    # If market is currently open, return the current open time today
    if is_market_open(market, after_utc):
        today_open = local_dt.replace(
            hour=session.open_time.hour,
            minute=session.open_time.minute,
            second=0, microsecond=0,
        )
        return today_open.astimezone(TZ_UTC)

    # Find next trading day
    candidate = local_dt.replace(
        hour=session.open_time.hour,
        minute=session.open_time.minute,
        second=0, microsecond=0,
    )

    # If we're past today's open, start from tomorrow
    if local_dt.time() >= session.open_time:
        candidate += timedelta(days=1)

    # Skip to next trading day
    while candidate.weekday() not in session.trading_days:
        candidate += timedelta(days=1)

    return candidate.astimezone(TZ_UTC)


def next_close(market: Market, after_utc: datetime | None = None) -> datetime:
    """Get the next market close time in UTC.

    Args:
        market: Which market.
        after_utc: Find next close after this time (defaults to now).

    Returns:
        Next close time as UTC datetime.
    """
    if market == Market.CRYPTO:
        # Crypto never closes; return a far-future sentinel
        return datetime(2099, 12, 31, tzinfo=TZ_UTC)

    session = SESSIONS[market]
    if after_utc is None:
        after_utc = datetime.now(TZ_UTC)

    local_dt = after_utc.astimezone(session.tz)

    # If market is currently open, close is today
    if is_market_open(market, after_utc):
        today_close = local_dt.replace(
            hour=session.close_time.hour,
            minute=session.close_time.minute,
            second=0, microsecond=0,
        )
        return today_close.astimezone(TZ_UTC)

    # Otherwise, find the next trading day's close
    nxt = next_open(market, after_utc)
    nxt_local = nxt.astimezone(session.tz)
    close_dt = nxt_local.replace(
        hour=session.close_time.hour,
        minute=session.close_time.minute,
        second=0, microsecond=0,
    )
    return close_dt.astimezone(TZ_UTC)


def market_status_summary(at_utc: datetime | None = None) -> list[dict]:
    """Get open/closed status for all markets.

    Returns a list of dicts with market info, suitable for API responses.
    """
    if at_utc is None:
        at_utc = datetime.now(TZ_UTC)

    results = []
    for market in Market:
        is_open = is_market_open(market, at_utc)
        session = SESSIONS[market]
        local_now = at_utc.astimezone(session.tz)

        entry = {
            "market": market.value,
            "is_open": is_open,
            "local_time": local_now.strftime("%H:%M %Z"),
            "timezone": str(session.tz),
        }

        if is_open and market != Market.CRYPTO:
            closes_at = next_close(market, at_utc)
            remaining = closes_at - at_utc
            entry["closes_in_minutes"] = int(remaining.total_seconds() / 60)
            entry["closes_at_utc"] = closes_at.isoformat()
        elif not is_open:
            opens_at = next_open(market, at_utc)
            until_open = opens_at - at_utc
            entry["opens_in_minutes"] = int(until_open.total_seconds() / 60)
            entry["opens_at_utc"] = opens_at.isoformat()

        results.append(entry)

    return results


def expected_candle_count(
    market: Market,
    timeframe: str,
    start_utc: datetime,
    end_utc: datetime,
) -> int:
    """Calculate expected number of candles between two timestamps.

    Accounts for market hours — stocks only have candles during sessions.
    Crypto is 24/7.

    Args:
        market: Which market.
        timeframe: Candle interval (1h, 1d, etc.).
        start_utc: Start of range (UTC).
        end_utc: End of range (UTC).

    Returns:
        Expected number of candles.
    """
    # Parse timeframe to timedelta
    tf_minutes = _timeframe_to_minutes(timeframe)

    if market == Market.CRYPTO:
        total_minutes = (end_utc - start_utc).total_seconds() / 60
        return int(total_minutes / tf_minutes)

    session = SESSIONS[market]
    holidays = MARKET_HOLIDAYS.get(market, set())

    def _is_trading_day(utc_dt: datetime) -> bool:
        local = utc_dt.astimezone(session.tz)
        return (
            local.weekday() in session.trading_days
            and local.date() not in holidays
        )

    # For daily candles, count trading days
    if timeframe == "1d":
        count = 0
        current = start_utc
        while current <= end_utc:
            if _is_trading_day(current):
                count += 1
            current += timedelta(days=1)
        return count

    # For intraday, count candles within each trading session
    session_minutes = (
        (session.close_time.hour * 60 + session.close_time.minute)
        - (session.open_time.hour * 60 + session.open_time.minute)
    )
    candles_per_session = session_minutes // tf_minutes

    # Count trading days in range
    trading_days = 0
    current = start_utc
    while current <= end_utc:
        if _is_trading_day(current):
            trading_days += 1
        current += timedelta(days=1)

    return trading_days * candles_per_session


def _timeframe_to_minutes(timeframe: str) -> int:
    """Convert timeframe string to minutes."""
    mapping = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }
    return mapping.get(timeframe, 60)
