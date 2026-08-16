"""
Broker-agnostic tick aggregation.

`BarBuilder` turns a stream of ticks into OHLCV candles and `splice` joins them
onto REST history. Neither knows anything about a specific broker - the Kite
socket lives in feed_kite.py and feeds these.

Three things this design takes seriously, because they are where tick-built
candles usually go wrong:

* **Volume.** Ticks give cumulative day volume, not per-bar volume. Summing the
  raw field would inflate every bar enormously, and the volume gate is the
  primary hard filter. Per-bar volume is therefore a *difference* of the
  cumulative counter, and any bar built from fewer than MIN_TICKS_PER_BAR ticks
  is marked unreliable rather than silently trusted.
* **Cold start.** A fresh connection has no history. Bars are spliced onto REST
  backfill, and the boundary bar belongs to exactly one source - never both.
* **Staleness.** A silent socket looks identical to a quiet market. If no tick
  arrives for WS_STALE_SECONDS the feed reports itself stale and the scanner
  falls back to REST instead of scanning frozen data.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from clock import MarketClock, now_naive
from config import cfg

logger = logging.getLogger(__name__)


def _floor_to_bar(ts: datetime, minutes: int, session_start=(9, 15)) -> datetime:
    """Bucket a tick into its bar, anchored to the 09:15 open."""
    anchor = ts.replace(hour=session_start[0], minute=session_start[1],
                        second=0, microsecond=0)
    if ts < anchor:
        anchor -= timedelta(days=1)
    elapsed = int((ts - anchor).total_seconds() // (minutes * 60))
    return anchor + timedelta(minutes=elapsed * minutes)


class BarBuilder:
    """Aggregates ticks into OHLCV bars for one instrument."""

    MIN_TICKS_PER_BAR = 3

    def __init__(self, symbol: str, minutes: int):
        self.symbol = symbol
        self.minutes = minutes
        self.current: Optional[Dict] = None
        self.completed: List[Dict] = []
        self.last_cum_volume: Optional[float] = None
        self.last_tick_at: Optional[datetime] = None

    def add_tick(self, price: float, cum_volume: Optional[float],
                 ts: datetime) -> Optional[Dict]:
        """Feed one tick. Returns a bar if this tick closed the previous one."""
        if price <= 0:
            return None
        bucket = _floor_to_bar(ts, self.minutes)
        self.last_tick_at = ts

        # Cumulative day volume -> per-bar volume. Resets (a new day, or a feed
        # restart) show up as a decrease; treat those as a fresh baseline
        # instead of emitting a negative volume.
        delta = 0.0
        if cum_volume is not None:
            if self.last_cum_volume is None or cum_volume < self.last_cum_volume:
                self.last_cum_volume = cum_volume
            else:
                delta = cum_volume - self.last_cum_volume
                self.last_cum_volume = cum_volume

        closed = None
        if self.current is None:
            self.current = self._new_bar(bucket, price, delta)
        elif bucket > self.current["timestamp"]:
            closed = self._finalize()
            self.current = self._new_bar(bucket, price, delta)
        elif bucket < self.current["timestamp"]:
            return None                                  # out-of-order tick
        else:
            bar = self.current
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += delta
            bar["ticks"] += 1
        return closed

    def _new_bar(self, bucket: datetime, price: float, volume: float) -> Dict:
        return {"timestamp": bucket, "open": price, "high": price, "low": price,
                "close": price, "volume": max(0.0, volume), "ticks": 1}

    def _finalize(self) -> Dict:
        bar = dict(self.current)
        bar["reliable"] = bar["ticks"] >= self.MIN_TICKS_PER_BAR
        if not bar["reliable"]:
            logger.debug("[WS] %s bar %s built from only %d tick(s)",
                         self.symbol, bar["timestamp"], bar["ticks"])
        self.completed.append(bar)
        if len(self.completed) > 400:
            self.completed = self.completed[-400:]
        return bar

    def frame(self, include_forming: bool = True) -> pd.DataFrame:
        rows = list(self.completed)
        if include_forming and self.current:
            rows.append(dict(self.current))
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows)
        return df[["timestamp", "open", "high", "low", "close", "volume"]]


def splice(rest_df: pd.DataFrame, ws_df: pd.DataFrame) -> pd.DataFrame:
    """
    Join REST backfill to streamed bars.

    The boundary bar is the trap: REST may hold a partially formed version of
    the same bar the stream is building. The streamed bar wins for any
    overlapping timestamp, so a bar is never counted twice or blended from two
    sources.
    """
    if ws_df is None or ws_df.empty:
        return rest_df
    if rest_df is None or rest_df.empty:
        return ws_df

    cutoff = ws_df["timestamp"].min()
    kept = rest_df[rest_df["timestamp"] < cutoff]
    out = pd.concat([kept, ws_df], ignore_index=True)
    return (out.sort_values("timestamp")
               .drop_duplicates(subset="timestamp", keep="last")
               .reset_index(drop=True))


def _first_num(row: Dict, keys: List[str], default: float = 0.0):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return default


def _tick_time(row: Dict) -> Optional[datetime]:
    import re
    raw = row.get("TickDt") or row.get("Time") or row.get("LastTradeTime")
    if raw is None:
        return None
    text = str(raw)
    match = re.search(r"/Date\((-?\d+)", text)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000)
        except (ValueError, OSError):
            return None
    if text.isdigit():
        try:
            value = int(text)
            if value > 10 ** 12:
                return datetime.fromtimestamp(value / 1000)
            if value > 10 ** 9:
                return datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return None
    return None
