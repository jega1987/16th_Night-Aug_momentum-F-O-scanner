"""
Market session helpers. All DB timestamps are stored as *naive IST* so that
SQLite and Postgres behave identically and comparisons never mix tz-aware
with tz-naive values.
"""
from datetime import datetime, time, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    """Timezone-aware 'now' in IST."""
    return datetime.now(IST)


def now_naive() -> datetime:
    """IST wall-clock time with tzinfo stripped - use this for the database."""
    return datetime.now(IST).replace(tzinfo=None)


def today_start() -> datetime:
    return now_naive().replace(hour=0, minute=0, second=0, microsecond=0)


class MarketClock:
    MARKET_OPEN = time(9, 15)
    MARKET_CLOSE = time(15, 30)
    SIGNAL_START = time(9, 30)   # skip the opening auction noise
    SIGNAL_END = time(15, 0)     # no fresh entries into the close
    SQUARE_OFF = time(15, 20)

    @classmethod
    def now(cls) -> datetime:
        return now_naive()

    @classmethod
    def is_market_day(cls, dt: datetime = None) -> bool:
        dt = dt or cls.now()
        # Weekday check only. Trading holidays are not encoded - add them to
        # HOLIDAYS below if you want the scanner to stay quiet on those days.
        return dt.weekday() < 5 and dt.date().isoformat() not in cls.HOLIDAYS

    # Extend this list each year from the exchange holiday circular.
    HOLIDAYS = set()

    @classmethod
    def is_market_open(cls, dt: datetime = None) -> bool:
        dt = dt or cls.now()
        return cls.is_market_day(dt) and cls.MARKET_OPEN <= dt.time() <= cls.MARKET_CLOSE

    @classmethod
    def can_take_new_signals(cls, dt: datetime = None) -> bool:
        dt = dt or cls.now()
        return cls.is_market_day(dt) and cls.SIGNAL_START <= dt.time() <= cls.SIGNAL_END

    @classmethod
    def should_square_off(cls, dt: datetime = None) -> bool:
        dt = dt or cls.now()
        return cls.is_market_day(dt) and dt.time() >= cls.SQUARE_OFF

    @classmethod
    def session_label(cls, dt: datetime = None) -> str:
        dt = dt or cls.now()
        if not cls.is_market_day(dt):
            return "CLOSED"
        t = dt.time()
        if t < cls.MARKET_OPEN:
            return "PRE-OPEN"
        if t > cls.MARKET_CLOSE:
            return "CLOSED"
        if t >= cls.SQUARE_OFF:
            return "SQUARE-OFF"
        if not cls.can_take_new_signals(dt):
            return "NO NEW ENTRIES"
        return "LIVE"

    @classmethod
    def bars_between(cls, start: datetime, end: datetime, bar_minutes: int) -> int:
        """Rough bar count between two timestamps, used for signal cooldowns."""
        if not start or not end:
            return 10 ** 6
        return int(max(0, (end - start).total_seconds()) // (bar_minutes * 60))


__all__ = ["IST", "MarketClock", "now_ist", "now_naive", "today_start", "timedelta"]
