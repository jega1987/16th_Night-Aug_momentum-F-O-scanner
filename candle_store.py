"""
Candle store.

The point everything else follows from: **a closed candle never changes.** The
10:15 bar is the same object at 10:20, at 15:30, and next Tuesday. Re-downloading
200 bars every five minutes to learn about one new bar is the waste.

So candles live in the database, and each cycle asks the broker only for the
window it doesn't already have.

Two things this does NOT fix, and it's worth being clear about them:

* It reduces *payload*, not *call count*. 5paisa's historical endpoint takes a
  date range, so the smallest possible request is still one day - one call per
  symbol per cycle either way. Fetching 75 bars instead of 200+ across 7 days is
  a big bandwidth saving and a small rate-limit saving.
* What actually cuts call count is `resample()` below: deriving 15-minute bars
  from stored 5-minute bars removes an entire API call per symbol per cycle, and
  guarantees the two timeframes agree with each other.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import and_

from clock import MarketClock, now_naive
from config import cfg
from database import Candle, session_scope
from feed_base import REQUIRED_COLUMNS

logger = logging.getLogger(__name__)

# Bars a full session produces, used to size backfill requests.
BARS_PER_SESSION = {"1m": 375, "3m": 125, "5m": 75, "10m": 38,
                    "15m": 25, "30m": 13, "60m": 7, "1d": 1}

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "60m": 60}


class CandleStore:
    def __init__(self, feed):
        self.feed = feed
        self.api_calls_saved = 0
        self.api_calls_made = 0

    # ------------------------------------------------------------------ #
    async def get(self, symbol: str, timeframe: str, bars: int,
                  use_futures: bool = True) -> pd.DataFrame:
        """
        Stored bars plus whatever is missing. Only the gap is fetched.
        """
        stored = self.read(symbol, timeframe, bars)
        last_ts = stored["timestamp"].iloc[-1] if len(stored) else None

        if not self._needs_refresh(last_ts, timeframe):
            self.api_calls_saved += 1
            logger.debug("[Store] %s %s served from cache (%d bars)", symbol, timeframe, len(stored))
            return stored.tail(bars).reset_index(drop=True)

        from_date = (last_ts.date() if last_ts is not None
                     else (now_naive() - timedelta(days=self._days_for(timeframe, bars))).date())
        fresh = await self.feed.get_historical(
            symbol, timeframe, bars, use_futures=use_futures, from_date=from_date)
        self.api_calls_made += 1

        self.write(symbol, timeframe, fresh)
        merged = self.read(symbol, timeframe, bars)
        return merged if len(merged) else fresh

    def _needs_refresh(self, last_ts, timeframe: str) -> bool:
        """A closed bar is final. Only fetch when a new one should exist."""
        if last_ts is None:
            return True
        minutes = TF_MINUTES.get(timeframe, 5)
        age = (now_naive() - last_ts).total_seconds() / 60
        if age < minutes:
            return False
        # Market shut and we already hold the last bar of the session.
        if not MarketClock.is_market_open() and last_ts.date() == now_naive().date():
            return False
        return True

    @staticmethod
    def _days_for(timeframe: str, bars: int) -> int:
        per_day = BARS_PER_SESSION.get(timeframe, 75)
        return max(7, int(bars / per_day * 1.8) + 3)

    # ------------------------------------------------------------------ #
    @staticmethod
    def read(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        with session_scope() as db:
            rows = (db.query(Candle)
                      .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
                      .order_by(Candle.timestamp.desc())
                      .limit(bars)
                      .all())
        if not rows:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        frame = pd.DataFrame([{
            "timestamp": r.timestamp, "open": r.open, "high": r.high,
            "low": r.low, "close": r.close, "volume": r.volume,
        } for r in reversed(rows)])
        return frame.reset_index(drop=True)

    @staticmethod
    def write(symbol: str, timeframe: str, df: pd.DataFrame) -> int:
        """
        Upsert. The most recent stored bar is always rewritten because it may
        have been mid-formation when we last saw it.
        """
        if df is None or df.empty:
            return 0
        written = 0
        with session_scope() as db:
            existing = {
                r[0] for r in db.query(Candle.timestamp)
                                .filter(Candle.symbol == symbol,
                                        Candle.timeframe == timeframe,
                                        Candle.timestamp >= df["timestamp"].min())
                                .all()
            }
            for row in df.itertuples(index=False):
                ts = pd.Timestamp(row.timestamp).to_pydatetime()
                if ts in existing:
                    (db.query(Candle)
                       .filter(and_(Candle.symbol == symbol,
                                    Candle.timeframe == timeframe,
                                    Candle.timestamp == ts))
                       .update({"open": float(row.open), "high": float(row.high),
                                "low": float(row.low), "close": float(row.close),
                                "volume": float(row.volume)}, synchronize_session=False))
                else:
                    db.add(Candle(symbol=symbol, timeframe=timeframe, timestamp=ts,
                                  open=float(row.open), high=float(row.high),
                                  low=float(row.low), close=float(row.close),
                                  volume=float(row.volume)))
                    written += 1
        return written

    # ------------------------------------------------------------------ #
    @staticmethod
    def resample(df: pd.DataFrame, to_timeframe: str,
                 session_start: str = "09:15") -> pd.DataFrame:
        """
        Build higher-timeframe bars from lower-timeframe ones.

        This is where the API savings actually come from - no separate 15m
        request per symbol per cycle. It also removes a class of bug: fetched 5m
        and 15m series can disagree at the boundary, and then the HTF filter is
        judging a slightly different market than the trigger.

        Bars are anchored to the 09:15 open so a 15-minute bar covers
        09:15-09:30, not 09:00-09:15.
        """
        if df is None or df.empty:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        minutes = TF_MINUTES.get(to_timeframe)
        if not minutes:
            raise ValueError(f"Cannot resample to '{to_timeframe}'")

        work = df.copy()
        work["timestamp"] = pd.to_datetime(work["timestamp"])
        work = work.set_index("timestamp").sort_index()

        first_day = work.index[0].normalize()
        hh, mm = (int(x) for x in session_start.split(":"))
        origin = first_day + pd.Timedelta(hours=hh, minutes=mm)

        agg = work.resample(f"{minutes}min", origin=origin, label="left", closed="left").agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))

        agg = agg.dropna(subset=["open", "high", "low", "close"])

        # Drop a leading partial bucket. If the stored history starts at 10:05,
        # the 10:00-10:15 bucket holds only two of its three source bars and is
        # not a valid 15-minute candle. The trailing bucket is left in place -
        # that one is the bar currently forming, and its close is the live price.
        if len(agg) and agg.index[0] < work.index[0]:
            agg = agg.iloc[1:]

        agg = agg.reset_index().rename(columns={"index": "timestamp"})
        if "timestamp" not in agg.columns:
            agg = agg.rename(columns={agg.columns[0]: "timestamp"})
        return agg[REQUIRED_COLUMNS].reset_index(drop=True)

    # ------------------------------------------------------------------ #
    async def backfill(self, symbols: List[str], timeframe: str = None,
                       bars: int = None) -> Dict[str, int]:
        """
        Pre-market bulk load. Run once before the open so the first scan of the
        day isn't competing with live trading for the rate limit.
        """
        timeframe = timeframe or cfg.TIMEFRAME
        bars = bars or cfg.HISTORY_BARS
        result = {}
        for symbol in symbols:
            try:
                df = await self.get(symbol, timeframe, bars)
                result[symbol] = len(df)
            except Exception as exc:
                logger.error("[Store] Backfill failed for %s: %s", symbol, exc)
                result[symbol] = 0
        total = sum(result.values())
        logger.info("[Store] Backfilled %d bars across %d symbol(s)", total, len(symbols))
        return result

    def stats(self) -> Dict:
        total = self.api_calls_made + self.api_calls_saved
        with session_scope() as db:
            stored = db.query(Candle).count()
        return {
            "candles_stored": stored,
            "api_calls_made": self.api_calls_made,
            "api_calls_saved": self.api_calls_saved,
            "hit_rate_pct": round(self.api_calls_saved / total * 100, 1) if total else 0.0,
        }

    @staticmethod
    def prune(keep_days: int = 30) -> int:
        """Old intraday bars are dead weight. Keep the window the filters need."""
        cutoff = now_naive() - timedelta(days=keep_days)
        with session_scope() as db:
            deleted = (db.query(Candle)
                         .filter(Candle.timestamp < cutoff)
                         .delete(synchronize_session=False))
        if deleted:
            logger.info("[Store] Pruned %d candle(s) older than %d days", deleted, keep_days)
        return deleted
