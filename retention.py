"""
Housekeeping: keep the database to what the scanner and dashboard use.

  scan_logs, index_snapshots, oi_snapshots  -> RETENTION_SCAN_DAYS (30)
  candles                                   -> RETENTION_CANDLE_DAYS (90),
                                               and never bars stamped after 15:29
  iv_snapshots                              -> IV_RANK_LOOKBACK_DAYS + 10
  signals, watch_signals                    -> RETENTION_SIGNAL_DAYS (400)

Signals are kept for over a year (at least the agreed 6 months). Runs daily
after the close; also runnable by hand: python retention.py [--dry-run]
"""
import logging
import sys
from datetime import timedelta
from typing import Dict

from sqlalchemy import extract, or_

from clock import now_naive
from config import cfg
from database import (Candle, IndexSnapshot, IVSnapshot, OISnapshot, ScanLog, Signal,
                      WatchSignal, session_scope)

logger = logging.getLogger(__name__)


def purge(dry_run: bool = False) -> Dict[str, int]:
    now = now_naive()
    plan = [
        ("scan_logs", ScanLog, ScanLog.timestamp, cfg.RETENTION_SCAN_DAYS),
        ("index_snapshots", IndexSnapshot, IndexSnapshot.timestamp, cfg.RETENTION_SCAN_DAYS),
        ("oi_snapshots", OISnapshot, OISnapshot.timestamp, cfg.RETENTION_SCAN_DAYS),
        ("candles", Candle, Candle.timestamp, cfg.RETENTION_CANDLE_DAYS),
        ("iv_snapshots", IVSnapshot, IVSnapshot.timestamp, cfg.IV_RANK_LOOKBACK_DAYS + 10),
        ("signals", Signal, Signal.timestamp, cfg.RETENTION_SIGNAL_DAYS),
        ("watch_signals", WatchSignal, WatchSignal.timestamp, cfg.RETENTION_SIGNAL_DAYS),
    ]
    out = {}
    for name, model, col, days in plan:
        if not days or days <= 0:
            continue
        cutoff = now - timedelta(days=days)
        with session_scope() as db:
            q = db.query(model).filter(col < cutoff)
            out[name] = q.count() if dry_run else q.delete(synchronize_session=False)
    # Post-close prints (15:30 and later) and pre-open bars in the candle cache.
    with session_scope() as db:
        hour, minute = extract("hour", Candle.timestamp), extract("minute", Candle.timestamp)
        q = db.query(Candle).filter(or_(hour > 15, (hour == 15) & (minute >= 30),
                                        hour < 9, (hour == 9) & (minute < 15)))
        out["candles_outside_session"] = q.count() if dry_run else q.delete(synchronize_session=False)
    logger.info("[Retention] %s %s", "would delete" if dry_run else "deleted", out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    purge(dry_run="--dry-run" in sys.argv)
