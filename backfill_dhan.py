"""
Replace the Kite backtest history (hist_candles) with Dhan data - in three
explicit steps, so the backtests are never left without history (15 Nov 2026).

  python backfill_dhan.py --load                  # 1. Dhan -> staging table hist_candles_dhan
  python backfill_dhan.py --verify                # 2. compare with Kite on overlapping days
  python backfill_dhan.py --swap --delete-kite    # 3. delete Kite rows, move Dhan rows in

  options: --days5 365 --days15 365 --daily 1825 --symbols "TCS,NIFTY 50"

What is loaded, per symbol (stocks: NSE cash; indices and INDIA VIX: index):
  5-minute and 15-minute candles for --days5 / --days15 days, daily for --daily.
Dhan serves intraday history for active instruments only, so (like Kite) the
underlying is the cash/spot series; expired futures intraday is not available
from either broker. The live scanner keeps using Kite and is not touched.

--load is resumable: (symbol, timeframe) already >= 90% complete is skipped.
--swap refuses to run without --delete-kite, or when verification failed.
"""
import argparse
import json
import logging
import sys
import time as _time
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import (Column, DateTime, Float, Index, Integer, String, UniqueConstraint,
                        func, text)

from clock import now_naive
from config import cfg
from database import Base, engine, init_db, session_scope
import backfill_history  # noqa: F401  (registers the hist_candles table)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s dhanfill  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("dhanfill")
logging.getLogger("httpx").setLevel(logging.WARNING)      # never log URLs (login has PIN/TOTP)
logging.getLogger("httpcore").setLevel(logging.WARNING)

VIX = "INDIA VIX"
SOURCE = "dhan_spot"
INTRADAY_SPAN = 88                      # Dhan allows 90 days per intraday call
BARS_PER_DAY = {"5m": 75, "15m": 25, "1d": 1}
TRADING_DAYS_PER_CAL_DAY = 250 / 365
VERIFY_MAX_MEDIAN_DIFF_PCT = 0.10       # median |close diff| above this fails a symbol
VERIFY_MIN_PASS_SHARE = 0.90            # share of symbols that must pass


class HistCandleDhan(Base):
    __tablename__ = "hist_candles_dhan"
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
    source = Column(String)
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_hist_candle_dhan"),
        Index("ix_hist_dhan_lookup", "symbol", "timeframe", "timestamp"),
    )


def _insert(rows: List[Dict], table=None) -> int:
    if not rows:
        return 0
    table = table if table is not None else HistCandleDhan.__table__
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    with engine.begin() as conn:
        for i in range(0, len(rows), 5000):
            conn.execute(insert(table).values(rows[i:i + 5000]).on_conflict_do_nothing(
                index_elements=["symbol", "timeframe", "timestamp"]))
    return len(rows)


def _rows(df: pd.DataFrame, symbol: str, timeframe: str) -> List[Dict]:
    out = []
    for r in df.itertuples(index=False):
        ts = pd.Timestamp(r.timestamp).to_pydatetime()
        if timeframe != "1d" and not (9 * 60 + 15 <= ts.hour * 60 + ts.minute < 15 * 60 + 30):
            continue
        out.append({"symbol": symbol, "timeframe": timeframe, "timestamp": ts,
                    "open": float(r.open), "high": float(r.high), "low": float(r.low),
                    "close": float(r.close), "volume": float(r.volume or 0),
                    "oi": (float(r.oi) if r.oi is not None and not pd.isna(r.oi) else None),
                    "source": SOURCE})
    return out


def _count(symbol: str, timeframe: str) -> int:
    with session_scope() as db:
        return int(db.query(func.count(HistCandleDhan.id))
                   .filter(HistCandleDhan.symbol == symbol, HistCandleDhan.timeframe == timeframe)
                   .scalar() or 0)


def symbol_list(only: List[str] = None) -> List[str]:
    if only:
        return only
    syms = set(cfg.INDICES) | {VIX}
    with engine.connect() as c:
        try:
            for (s,) in c.execute(text("SELECT DISTINCT symbol FROM hist_candles "
                                       "WHERE symbol NOT LIKE '%::FUT'")):
                syms.add(s)
        except Exception:
            pass
    return sorted(syms)


# ------------------------------------------------------------------ load
def load(dhan, symbols: List[str], days5: int, days15: int, daily: int) -> Dict:
    inst = dhan.instrument_map(symbols)
    missing = [s for s in symbols if s not in inst]
    if missing:
        logger.warning("No Dhan instrument for %d symbol(s): %s", len(missing), ", ".join(missing))
    end = now_naive()
    stats, t0 = {}, _time.time()
    for n, sym in enumerate(symbols, 1):
        if sym not in inst:
            continue
        sid, seg, kind = inst[sym]
        stats[sym] = {}
        for tf, days in (("5m", days5), ("15m", days15), ("1d", daily)):
            if days <= 0:
                continue
            expected = int(days * TRADING_DAYS_PER_CAL_DAY * BARS_PER_DAY[tf])
            have = _count(sym, tf)
            if expected and have >= 0.9 * expected:
                stats[sym][tf] = have
                continue
            got, failed = 0, 0
            if tf == "1d":
                start, step = end - timedelta(days=days), timedelta(days=365)
            else:
                start, step = end - timedelta(days=days), timedelta(days=INTRADAY_SPAN)
            cur = start
            while cur < end:
                nxt = min(end, cur + step)
                try:
                    if tf == "1d":
                        df = dhan.daily(sid, seg, kind, cur.date(), (nxt + timedelta(days=1)).date())
                    else:
                        df = dhan.intraday(sid, seg, kind, cur, nxt, int(tf[:-1]))
                    got += _insert(_rows(df, sym, tf))
                except Exception as exc:
                    if getattr(exc, "is_auth", False):
                        raise
                    # A chunk before the listing date (new stocks) has no data:
                    # skip it and carry on with the later chunks.
                    failed += 1
                    logger.debug("%s %s %s: %s", sym, tf, cur.date(), exc)
                cur = nxt
            if failed and not got:
                logger.warning("%s %s: no data from Dhan (%d chunk(s) failed)", sym, tf, failed)
            stats[sym][tf] = got
        if n % 10 == 0 or n == len(symbols):
            logger.info("Progress %d/%d | %d Dhan calls | %.1f min", n, len(symbols), dhan.calls,
                        (_time.time() - t0) / 60)
    return {"symbols": len(stats), "missing": missing, "calls": dhan.calls,
            "minutes": round((_time.time() - t0) / 60, 1)}


# ---------------------------------------------------------------- verify
def verify(timeframe: str = "5m") -> Dict:
    """Close / volume agreement with the Kite rows on overlapping timestamps."""
    q = text("""SELECT d.symbol, d.close AS dc, k.close AS kc, d.volume AS dv, k.volume AS kv
                FROM hist_candles_dhan d JOIN hist_candles k
                  ON k.symbol = d.symbol AND k.timeframe = d.timeframe AND k.timestamp = d.timestamp
                WHERE d.timeframe = :tf AND k.source = 'spot'""")
    with engine.connect() as c:
        df = pd.read_sql(q, c, params={"tf": timeframe})
    if df.empty:
        return {"ok": False, "reason": "no overlapping rows"}
    df["diff_pct"] = (df.dc - df.kc).abs() / df.kc * 100
    df["vol_ratio"] = np.where(df.kv > 0, df.dv / df.kv.replace(0, np.nan), np.nan)
    per = df.groupby("symbol").agg(n=("dc", "size"), med_diff=("diff_pct", "median"),
                                   p99_diff=("diff_pct", lambda x: np.percentile(x, 99)),
                                   vol_ratio=("vol_ratio", "median"))
    per["pass"] = per.med_diff <= VERIFY_MAX_MEDIAN_DIFF_PCT
    share = float(per["pass"].mean())
    with engine.connect() as c:
        dh = dict(c.execute(text("SELECT timeframe, count(*) FROM hist_candles_dhan GROUP BY 1")).all())
        span = c.execute(text("SELECT min(timestamp), max(timestamp) FROM hist_candles_dhan "
                              "WHERE timeframe = '5m'")).one()
    return {"ok": share >= VERIFY_MIN_PASS_SHARE, "symbols_compared": int(len(per)),
            "pass_share": round(share, 3), "rows_compared": int(len(df)),
            "median_close_diff_pct": round(float(df.diff_pct.median()), 4),
            "p99_close_diff_pct": round(float(np.percentile(df.diff_pct, 99)), 4),
            "median_volume_ratio": round(float(np.nanmedian(df.vol_ratio)), 3),
            "worst": per.sort_values("med_diff", ascending=False).head(8).round(4).reset_index().to_dict("records"),
            "dhan_rows": {k: int(v) for k, v in dh.items()},
            "dhan_5m_span": [str(span[0]), str(span[1])]}


# ------------------------------------------------------------------ swap
def _db_size() -> str:
    if engine.dialect.name != "postgresql":
        return "n/a"
    with engine.connect() as c:
        return c.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).scalar()


def swap(delete_kite: bool) -> Dict:
    """
    1. verify (needs the Kite rows), 2. delete the Kite rows and VACUUM so their
    space is reused (the database volume is 5 GB), 3. move the Dhan rows in,
    4. drop the staging table. Safe to re-run if it stops after step 2: with no
    Kite rows left (they are only deleted after a passing verification) it goes
    straight to the move.
    """
    if not delete_kite:
        raise SystemExit("--swap deletes the Kite history; pass --delete-kite to confirm")
    with engine.connect() as c:
        kite = dict(c.execute(text("SELECT source, count(*) FROM hist_candles "
                                   "WHERE source IS NULL OR source <> :s GROUP BY 1"), {"s": SOURCE}).all())
    if kite:
        rep = verify()
        if not rep.get("ok"):
            raise SystemExit(f"verification failed, not swapping: {json.dumps(rep, default=str)[:600]}")
        with engine.begin() as c:
            c.execute(text("DELETE FROM hist_candles WHERE source IS NULL OR source <> :s"), {"s": SOURCE})
        logger.info("Deleted Kite rows %s; database %s", kite, _db_size())
        if engine.dialect.name == "postgresql":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.execute(text("VACUUM hist_candles"))
    with engine.begin() as c:
        moved = c.execute(text(
            "INSERT INTO hist_candles (symbol, timeframe, timestamp, open, high, low, close, volume, oi, source) "
            "SELECT symbol, timeframe, timestamp, open, high, low, close, volume, oi, source "
            "FROM hist_candles_dhan WHERE 1=1 ON CONFLICT (symbol, timeframe, timestamp) DO NOTHING")).rowcount
        c.execute(text("DROP TABLE hist_candles_dhan"))
    if engine.dialect.name == "postgresql":
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("VACUUM ANALYZE hist_candles"))
    return {"kite_rows_deleted": {k or "none": int(v) for k, v in kite.items()},
            "dhan_rows_moved": int(moved), "database_size": _db_size()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replace the Kite backtest history with Dhan data")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--swap", action="store_true")
    ap.add_argument("--delete-kite", action="store_true")
    ap.add_argument("--days5", type=int, default=365)
    ap.add_argument("--days15", type=int, default=365)
    ap.add_argument("--daily", type=int, default=1825)
    ap.add_argument("--symbols", type=str, default="")
    a = ap.parse_args(argv)
    init_db()
    HistCandleDhan.__table__.create(bind=engine, checkfirst=True)
    logger.info("Database size at start: %s", _db_size())
    if a.load:
        from dhan_data import DhanData
        dhan = DhanData()
        dhan.login()
        syms = symbol_list([s.strip() for s in a.symbols.split(",") if s.strip()])
        logger.info("LOAD %s", json.dumps(load(dhan, syms, a.days5, a.days15, a.daily)))
    if a.verify:
        logger.info("VERIFY %s", json.dumps(verify(), default=str))
    if a.swap:
        logger.info("SWAP %s", json.dumps(swap(a.delete_kite), default=str))
    logger.info("DHANFILL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
