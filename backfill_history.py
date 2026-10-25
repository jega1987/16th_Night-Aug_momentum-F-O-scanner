"""
One-off historical download for backtesting and model training.

    python backfill_history.py                 # defaults below
    python backfill_history.py --days15 365 --days5 180 --daily 730
    python backfill_history.py --symbols "NIFTY 50,TCS"   # a subset

What it stores, in its own table `hist_candles` (NOT the live `candles`
table, which is pruned to CANDLE_RETENTION_DAYS):
  - 15-minute candles for the last --days15 days
  - 5-minute candles for the last --days5 days
  - daily candles for the last --daily days
for every symbol in the latest stored universe plus the configured indices,
plus INDIA VIX (15-minute and daily) for regime studies.

Source per symbol, in order of preference:
  1. continuous futures (`continuous=True`, with OI) - the same instrument the
     live scanner trades on, stitched across expiries by Kite;
  2. the cash/spot instrument (stocks keep volume, indices have none).
The chosen source is recorded on every row, so the backtest can tell them apart.

Safe to re-run: rows are inserted with ON CONFLICT DO NOTHING and any
(symbol, timeframe) that is already >= 90% complete is skipped.

Survivorship note: the symbol list is TODAY's universe. Stocks that left F&O
during the period are missing, which flatters backtest results slightly.
"""
import argparse
import asyncio
import json
import logging
import sys
import time as _time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import (Column, DateTime, Float, Index, Integer, String,
                        UniqueConstraint, func)

from clock import now_naive
from config import cfg
from database import Base, engine, init_db, session_scope

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s backfill  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backfill")

KITE_INTERVAL = {"5m": "5minute", "15m": "15minute", "1d": "day"}
# Kite's per-request span ceilings (days), kept one day inside the limit.
SPAN_DAYS = {"5m": 99, "15m": 199, "1d": 1999}
BARS_PER_DAY = {"5m": 75, "15m": 25, "1d": 1}
TRADING_DAYS_PER_CAL_DAY = 250 / 365
VIX_SYMBOL = "INDIA VIX"


class HistCandle(Base):
    __tablename__ = "hist_candles"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    oi = Column(Float)
    source = Column(String)          # "fut_continuous" | "spot"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_hist_candle"),
        Index("ix_hist_lookup", "symbol", "timeframe", "timestamp"),
    )


# --------------------------------------------------------------------------- #
def _insert_rows(rows: List[Dict]) -> int:
    """Bulk insert, ignoring rows that already exist. Returns rows attempted."""
    if not rows:
        return 0
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    table = HistCandle.__table__
    with engine.begin() as conn:
        for i in range(0, len(rows), 5000):
            stmt = insert(table).values(rows[i:i + 5000]).on_conflict_do_nothing(
                index_elements=["symbol", "timeframe", "timestamp"])
            conn.execute(stmt)
    return len(rows)


def _stored_count(symbol: str, timeframe: str) -> int:
    with session_scope() as db:
        return int(db.query(func.count(HistCandle.id))
                   .filter(HistCandle.symbol == symbol, HistCandle.timeframe == timeframe)
                   .scalar() or 0)


def _chunks(start: datetime, end: datetime, span_days: int) -> List[Tuple[datetime, datetime]]:
    out, cur = [], start
    while cur < end:
        nxt = min(end, cur + timedelta(days=span_days))
        out.append((cur, nxt))
        cur = nxt + timedelta(seconds=1)
    return out


def _to_rows(raw: List[Dict], symbol: str, timeframe: str, source: str) -> List[Dict]:
    rows = []
    for r in raw or []:
        ts = pd.Timestamp(r["date"])
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)          # naive IST, same as the live tables
        rows.append({
            "symbol": symbol, "timeframe": timeframe, "timestamp": ts.to_pydatetime(),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r.get("volume") or 0),
            "oi": (float(r["oi"]) if r.get("oi") is not None else None),
            "source": source,
        })
    return rows


# --------------------------------------------------------------------------- #
class Backfiller:
    def __init__(self, feed):
        self.feed = feed
        self.stats: Dict[str, Dict] = {}
        self.failures: List[str] = []
        self.calls = 0

    def _instrument(self, symbol: str, futures: bool) -> Optional[Dict]:
        if symbol == VIX_SYMBOL:
            if futures:
                return None
            for row in self.feed._instruments.get("NSE", []):
                if str(row.get("tradingsymbol", "")).upper() == VIX_SYMBOL:
                    return row
            return None
        try:
            return self.feed._instrument(symbol, use_futures=futures)
        except Exception:
            return None

    async def _fetch(self, token: int, start: datetime, end: datetime, timeframe: str,
                     continuous: bool, label: str) -> List[Dict]:
        kite = self.feed.kite
        interval = KITE_INTERVAL[timeframe]
        self.calls += 1
        return await self.feed._call(
            lambda: kite.historical_data(token, start, end, interval,
                                         continuous=continuous, oi=True),
            label, attempts=3, base_delay=2.0) or []

    async def _pull_series(self, symbol: str, timeframe: str, days: int) -> Tuple[int, str]:
        """Returns (rows stored, source). Tries continuous futures, then spot."""
        end = now_naive()
        start = end - timedelta(days=days)
        expected = int(days * TRADING_DAYS_PER_CAL_DAY * BARS_PER_DAY[timeframe])
        have = _stored_count(symbol, timeframe)
        if expected and have >= 0.9 * expected:
            return have, "already stored"

        # Kite serves continuous futures for DAILY candles only ("invalid
        # interval for continuous data" otherwise), and not for BSE (BFO).
        attempts = (((True, "fut_continuous"), (False, "spot")) if timeframe == "1d"
                    else ((False, "spot"),))
        for futures, source in attempts:
            inst = self._instrument(symbol, futures)
            if not inst:
                continue
            token = int(inst["instrument_token"])
            total, ok = 0, True
            for a, b in _chunks(start, end, SPAN_DAYS[timeframe]):
                label = f"hist {symbol} {timeframe} {source} {a:%Y-%m-%d}"
                try:
                    raw = await self._fetch(token, a, b, timeframe, futures, label)
                except Exception as exc:
                    logger.warning("%s -> %s", label, exc)
                    ok = False
                    break
                total += _insert_rows(_to_rows(raw, symbol, timeframe, source))
            # Continuous futures that come back nearly empty are treated as
            # unsupported for this interval and the spot series is used instead.
            if ok and total >= 0.5 * expected:
                return total, source
            if ok and total and not futures:
                return total, source
            if futures:
                # Drop a partial futures series so spot rows are not blocked
                # by the unique constraint and the two are never mixed.
                with session_scope() as db:
                    db.query(HistCandle).filter(
                        HistCandle.symbol == symbol, HistCandle.timeframe == timeframe,
                        HistCandle.source == source).delete(synchronize_session=False)
        return 0, "failed"

    async def _pull_current_future(self, symbol: str, timeframe: str = "5m") -> Tuple[int, str]:
        """
        5-minute candles of the CURRENT futures contract, stored as
        '<symbol>::FUT'. Expired contracts are not in the instruments dump, so
        intraday OI (and index volume) only exists for this contract's life.
        """
        if symbol == VIX_SYMBOL:
            return 0, "n/a"
        inst = self._instrument(symbol, True)
        if not inst:
            return 0, "no future"
        key = f"{symbol}::FUT"
        end = now_naive()
        start = end - timedelta(days=SPAN_DAYS[timeframe])
        label = f"hist {key} {timeframe}"
        try:
            raw = await self._fetch(int(inst["instrument_token"]), start, end, timeframe,
                                    False, label)
        except Exception as exc:
            logger.warning("%s -> %s", label, exc)
            return 0, "failed"
        n = _insert_rows(_to_rows(raw, key, timeframe, "fut_current"))
        return n, "fut_current"

    async def run(self, symbols: List[str], days15: int, days5: int, daily: int) -> Dict:
        plan = [("15m", days15), ("5m", days5), ("1d", daily)]
        t0 = _time.time()
        for i, symbol in enumerate(symbols, 1):
            self.stats[symbol] = {}
            for timeframe, days in plan:
                if days <= 0 or (symbol == VIX_SYMBOL and timeframe == "5m"):
                    continue
                try:
                    n, source = await self._pull_series(symbol, timeframe, days)
                except Exception as exc:
                    n, source = 0, f"error: {exc}"
                    if "expired" in str(exc).lower():
                        raise
                self.stats[symbol][timeframe] = {"rows": n, "source": source}
                if n == 0:
                    self.failures.append(f"{symbol} {timeframe}: {source}")
            if days5 > 0 and symbol != VIX_SYMBOL:
                n, source = await self._pull_current_future(symbol)
                self.stats[symbol]["5m_fut"] = {"rows": n, "source": source}
            if i % 10 == 0 or i == len(symbols):
                logger.info("Progress %d/%d symbols | %d API calls | %.1f min",
                            i, len(symbols), self.calls, (_time.time() - t0) / 60)
        return self.summary(t0)

    def summary(self, t0: float) -> Dict:
        by_source: Dict[str, int] = {}
        rows = 0
        for tfs in self.stats.values():
            for v in tfs.values():
                rows += v["rows"]
                by_source[v["source"]] = by_source.get(v["source"], 0) + 1
        with session_scope() as db:
            total = int(db.query(func.count(HistCandle.id)).scalar() or 0)
            span = db.query(func.min(HistCandle.timestamp), func.max(HistCandle.timestamp)).one()
        size = None
        if engine.dialect.name == "postgresql":
            with engine.connect() as conn:
                size = conn.exec_driver_sql(
                    "SELECT pg_size_pretty(pg_total_relation_size('hist_candles'))").scalar()
        return {
            "symbols": len(self.stats), "api_calls": self.calls,
            "minutes": round((_time.time() - t0) / 60, 1),
            "series_by_source": by_source, "rows_this_run": rows,
            "rows_in_table": total,
            "from": str(span[0]) if span[0] else None, "to": str(span[1]) if span[1] else None,
            "table_size": size, "failed_series": self.failures[:50],
            "failed_count": len(self.failures),
        }


# --------------------------------------------------------------------------- #
async def connect_kite():
    from feed_kite import CredentialsError, KiteFeed
    feed = KiteFeed()
    try:
        await feed.connect()
        return feed
    except CredentialsError as exc:
        if not cfg.KITE_AUTO_LOGIN:
            raise
        logger.info("Stored token unusable (%s) - running TOTP auto-login", exc)
    from kite_autologin import auto_login
    await asyncio.to_thread(auto_login)
    feed = KiteFeed()
    await feed.connect()
    return feed


def universe_symbols(limit: int) -> List[str]:
    from universe import UniverseBuilder
    out = list(cfg.INDICES)
    for s in UniverseBuilder.load(limit=limit):
        if s not in out:
            out.append(s)
    return out


def start_health_server() -> None:
    """
    Answer Railway's /health check while the download runs. The shared
    railway.json gives every service a health check; without a listener the
    deployment would be marked failed and stopped part-way through.
    """
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(os.getenv("PORT", "0") or 0)
    if not port:
        return

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "role": "history-backfill"}')

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), Health)
    except OSError as exc:
        logger.warning("Health endpoint not started (%s) - continuing without it", exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health endpoint listening on :%d", port)


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--days15", type=int, default=365)
    ap.add_argument("--days5", type=int, default=180)
    ap.add_argument("--daily", type=int, default=730)
    ap.add_argument("--limit", type=int, default=200, help="max universe stocks")
    ap.add_argument("--symbols", type=str, default="", help="comma-separated subset")
    ap.add_argument("--no-vix", action="store_true")
    args = ap.parse_args(argv)

    start_health_server()
    init_db()
    HistCandle.__table__.create(bind=engine, checkfirst=True)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               or universe_symbols(args.limit))
    if not args.no_vix and VIX_SYMBOL not in symbols:
        symbols.append(VIX_SYMBOL)
    logger.info("Backfill start: %d symbols | 15m %dd, 5m %dd, daily %dd",
                len(symbols), args.days15, args.days5, args.daily)

    feed = await connect_kite()
    result = await Backfiller(feed).run(symbols, args.days15, args.days5, args.daily)
    logger.info("BACKFILL SUMMARY %s", json.dumps(result, default=str))
    return 0 if result["rows_in_table"] > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
