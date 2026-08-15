"""
WebSocket tick streaming and candle construction.

One morning handshake, then a single streaming connection replaces the
per-cycle REST polling. Verified against py5paisa 0.7.x:

    payload = client.Request_Feed("mf", "s", req_list)   # "u" to unsubscribe
    client.connect(payload)          # builds WebSocketApp, on_open sends payload
    client.error_data(on_error)      # must be set BEFORE receive_data
    client.receive_data(on_message)  # calls run_forever() - BLOCKS, needs a thread
    client.close_data()

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
import json
import logging
import threading
import time
from collections import defaultdict
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


class TickStream:
    """
    Owns the socket. Runs run_forever() on a worker thread and publishes bars
    into BarBuilders that the async side reads.
    """

    def __init__(self, client, symbols_to_scrips: Dict[str, Dict], minutes: int = 5):
        self.client = client
        self.map = symbols_to_scrips                     # symbol -> {Exch, ExchType, ScripCode}
        self.by_token = {int(v["ScripCode"]): k for k, v in symbols_to_scrips.items()}
        self.minutes = minutes
        self.builders: Dict[str, BarBuilder] = {
            sym: BarBuilder(sym, minutes) for sym in symbols_to_scrips
        }
        self.ltp: Dict[str, float] = {}
        self.connected = False
        self.subscribed = False
        self.stopping = False
        self.last_message_at: Optional[datetime] = None
        self.tick_count = 0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stopping = False
        self._thread = threading.Thread(target=self._run, name="tickstream", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self.stopping:
            try:
                req_list = [self.map[s] for s in self.map]
                payload = self.client.Request_Feed("mf", "s", req_list)
                self.client.connect(payload)
                self.client.error_data(self._on_error)
                self.connected = True
                self.subscribed = True
                logger.info("[WS] Subscribed to %d instrument(s)", len(req_list))
                self.client.receive_data(self._on_message)   # blocks until closed
            except Exception as exc:
                self.error = str(exc)
                logger.error("[WS] Stream failed: %s", exc)
            finally:
                self.connected = False

            if self.stopping or not MarketClock.is_market_open():
                break
            logger.info("[WS] Reconnecting in %ds", cfg.WS_RECONNECT_SECONDS)
            time.sleep(cfg.WS_RECONNECT_SECONDS)

    # ------------------------------------------------------------------ #
    def _on_message(self, *args) -> None:
        """websocket-client passes (ws, message); older builds pass (message,)."""
        message = args[-1] if args else None
        if not message:
            return
        try:
            payload = json.loads(message) if isinstance(message, (str, bytes)) else message
        except (ValueError, TypeError):
            return

        rows = payload if isinstance(payload, list) else [payload]
        self.last_message_at = now_naive()
        for row in rows:
            if isinstance(row, dict):
                self._apply(row)

    def _apply(self, row: Dict) -> None:
        token = row.get("Token") or row.get("ScripCode")
        try:
            symbol = self.by_token.get(int(token))
        except (TypeError, ValueError):
            return
        if not symbol:
            return

        price = _first_num(row, ["LastRate", "LastTradedPrice", "LTP", "Rate"])
        if price <= 0:
            return
        cum_volume = _first_num(row, ["TotalQty", "Volume", "TotalQtyTraded"], default=None)
        ts = _tick_time(row) or now_naive()

        with self._lock:
            self.ltp[symbol] = price
            self.tick_count += 1
            builder = self.builders.get(symbol)
            if builder:
                builder.add_tick(price, cum_volume, ts)

    def _on_error(self, *args) -> None:
        self.error = str(args[-1]) if args else "unknown websocket error"
        logger.error("[WS] %s", self.error)

    # ------------------------------------------------------------------ #
    def unsubscribe(self, symbols: List[str] = None) -> bool:
        """
        Stop consuming the feed. The requirement calls for this once the daily
        signal cap is hit - there is nothing left to act on, so paying for the
        stream is waste.
        """
        targets = symbols or list(self.map)
        req_list = [self.map[s] for s in targets if s in self.map]
        if not req_list:
            return False
        try:
            payload = self.client.Request_Feed("mf", "u", req_list)
            ws = getattr(self.client, "ws", None)
            if ws is not None:
                ws.send(json.dumps(payload))
            self.subscribed = False
            logger.info("[WS] Unsubscribed from %d instrument(s)", len(req_list))
            return True
        except Exception as exc:
            logger.error("[WS] Unsubscribe failed: %s", exc)
            return False

    def stop(self) -> None:
        self.stopping = True
        try:
            self.client.close_data()
        except Exception:
            pass
        self.connected = False
        self.subscribed = False

    # ------------------------------------------------------------------ #
    def is_stale(self) -> bool:
        """A silent socket and a quiet market look identical. Assume the worst."""
        if not self.connected:
            return True
        if self.last_message_at is None:
            return True
        return (now_naive() - self.last_message_at).total_seconds() > cfg.WS_STALE_SECONDS

    def frame(self, symbol: str) -> Optional[pd.DataFrame]:
        with self._lock:
            builder = self.builders.get(symbol)
            return builder.frame() if builder else None

    def status(self) -> Dict:
        return {
            "connected": self.connected,
            "subscribed": self.subscribed,
            "instruments": len(self.map),
            "ticks": self.tick_count,
            "stale": self.is_stale(),
            "last_message": self.last_message_at.isoformat() if self.last_message_at else None,
            "error": self.error,
        }


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
            # 5paisa also emits seconds since the 1980 epoch on some feeds.
            if value > 10 ** 12:
                return datetime.fromtimestamp(value / 1000)
            if value > 10 ** 9:
                return datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return None
    return None
