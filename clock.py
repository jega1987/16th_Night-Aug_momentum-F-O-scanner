"""
Market session helpers. All DB timestamps are stored as *naive IST* so that
SQLite and Postgres behave identically and comparisons never mix tz-aware
with tz-naive values.
"""
from datetime import datetime, time, timedelta

import pytz

from config import cfg

IST = pytz.timezone("Asia/Kolkata")


def _parse_time(raw: str, default: time) -> time:
    """'09:45' -> time(9, 45). Anything malformed falls back to the default."""
    try:
        hh, mm = (int(x) for x in str(raw).strip().split(":")[:2])
        return time(hh, mm)
    except (TypeError, ValueError):
        return default


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
    # Entry window, from SIGNAL_START_TIME / SIGNAL_END_TIME in config. The
    # defaults skip the opening rotation and stop fresh entries early enough
    # that a 4R target has more than an hour before the square-off bell.
    SIGNAL_START = _parse_time(cfg.SIGNAL_START_TIME, time(9, 45))
    SIGNAL_END = _parse_time(cfg.SIGNAL_END_TIME, time(14, 15))
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

    # From MARKET_HOLIDAYS in config (ISO dates). Extend it each year from
    # the exchange holiday circular.
    HOLIDAYS = {d.strip() for d in cfg.MARKET_HOLIDAYS if d.strip()}

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
