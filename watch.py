"""
Watch-only setups.

A watched setup (today: the tight-range box) is evaluated on every scan with
exactly the same checks as the live rules - volume, ADX, RSI, gap filter,
score - but with its own coil detector. When the live rules reject a bar and
the watched setup passes, the signal is stored in `watch_signals`. It is
never traded and never counted in the P&L.

Each open watch signal is then scored from stored 5-minute candles with the
live trade plan: stop, three targets with the 50/30/20 scale-out, stop to
breakeven at TP1 and to TP1 at TP2, and the same positional time stop. There
is no Supertrend trail, so results are a yardstick for comparing setups,
not a replica of live management. A bar that touches both the stop and a
target counts as the stop.
"""
import logging
from contextlib import contextmanager
from datetime import time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from clock import MarketClock, _parse_time, now_naive
from config import cfg
from database import Candle, WatchSignal, session_scope

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return cfg.WATCH_DETECTOR in ("box",)


@contextmanager
def watched_rules():
    """
    Swap the coil detector for the watched one. Only for synchronous code:
    the scanner runs symbols concurrently on one event loop, so there must be
    no await while this is active.
    """
    saved = (cfg.SQUEEZE_DETECTOR, cfg.BOX_MAX_WIDTH_ATR)
    cfg.SQUEEZE_DETECTOR = cfg.WATCH_DETECTOR
    cfg.BOX_MAX_WIDTH_ATR = cfg.WATCH_BOX_WIDTH_ATR
    try:
        yield
    finally:
        cfg.SQUEEZE_DETECTOR, cfg.BOX_MAX_WIDTH_ATR = saved


def already_watching(symbol: str, direction: str, when=None) -> bool:
    """One watch signal per symbol and direction per day, none while one is open."""
    when = when or now_naive()
    day = when.replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as db:
        q = db.query(WatchSignal).filter(WatchSignal.symbol == symbol)
        if q.filter(WatchSignal.status == "OPEN").count():
            return True
        return bool(q.filter(WatchSignal.direction == direction,
                             WatchSignal.timestamp >= day).count())


def record(symbol: str, result, levels: Dict, bar_time, live_reason: str) -> Optional[int]:
    m = result.meta
    with session_scope() as db:
        row = WatchSignal(
            timestamp=now_naive(), bar_time=pd.Timestamp(bar_time).to_pydatetime(),
            setup=m.get("setup") or cfg.WATCH_DETECTOR, symbol=symbol,
            direction=result.direction, entry=levels["entry"], sl=levels["sl"],
            tp1=levels["tp1"], tp2=levels["tp2"], tp3=levels["tp3"],
            range_high=m.get("squeeze_high"), range_low=m.get("squeeze_low"),
            coil_bars=m.get("squeeze_bars"), adx_value=m.get("adx_value"),
            rsi_value=m.get("rsi_value"), vol_ratio=m.get("vol_ratio"),
            composite_score=result.scores.get("composite"),
            live_reason=(live_reason or "")[:240], status="OPEN",
        )
        db.add(row)
        db.flush()
        rid = row.id
    logger.info("[Watch] %s %s %s @ %.2f | SL %.2f | TP %.2f/%.2f/%.2f (live: %s)",
                (m.get("setup") or cfg.WATCH_DETECTOR).upper(), symbol, result.direction,
                levels["entry"], levels["sl"], levels["tp1"], levels["tp2"], levels["tp3"],
                live_reason)
    return rid


# --------------------------------------------------------------------------- #
def score(sig: Dict, bars: pd.DataFrame, now=None) -> Dict:
    """
    Walk the 5-minute bars after the signal bar. Returns the fields to update:
    status, outcome, r_multiple, mfe_r, exit_price, exit_time.
    """
    now = now or now_naive()
    long_side = sig["direction"] == "LONG"
    sign = 1 if long_side else -1
    entry = float(sig["entry"])
    risk = abs(entry - float(sig["sl"]))
    if risk <= 0:
        return {"status": "CLOSED", "outcome": "INVALID", "r_multiple": 0.0}
    f1, f2, f3 = cfg.SCALE_OUT
    tps = [float(sig["tp1"]), float(sig["tp2"]), float(sig["tp3"])]
    stop = float(sig["sl"])
    exit_t = _parse_time(cfg.POS_EXIT_TIME, time(15, 20))
    entry_day = pd.Timestamp(sig["bar_time"]).date()

    def leg(px):
        return (px - entry) * sign / risk

    realized, open_frac, hit, best = 0.0, 1.0, 0, entry
    after = bars[pd.to_datetime(bars["timestamp"]) > pd.Timestamp(sig["bar_time"])]
    for _, b in after.iterrows():
        ts = pd.Timestamp(b["timestamp"])
        if ts.time() >= time(15, 30):            # post-close prints are not tradable
            continue
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        best = max(best, h) if long_side else min(best, l)
        # stop first (a gap through it fills at the open)
        if (long_side and o <= stop) or (not long_side and o >= stop):
            px = o
        elif (long_side and l <= stop) or (not long_side and h >= stop):
            px = stop
        else:
            px = None
        if px is not None:
            realized += open_frac * leg(px)
            outcome = "SL" if hit == 0 else ("BE" if hit == 1 else "TP2")
            return _closed(outcome, realized, best, entry, risk, sign, px, ts)
        # targets in order
        reach = (lambda p: h >= p) if long_side else (lambda p: l <= p)
        if hit == 0 and reach(tps[0]):
            realized += f1 * leg(tps[0]); open_frac -= f1; hit = 1
            stop = entry
        if hit == 1 and reach(tps[1]):
            realized += f2 * leg(tps[1]); open_frac -= f2; hit = 2
            stop = tps[0]
        if hit == 2 and reach(tps[2]):
            realized += open_frac * leg(tps[2])
            return _closed("TP3", realized, best, entry, risk, sign, tps[2], ts)
        # time stop: POS_MAX_HOLD_DAYS trading days after the entry day, at POS_EXIT_TIME
        close_ts = ts + timedelta(minutes=5)
        held = MarketClock.trading_days_between(entry_day, close_ts.date())
        if held > cfg.POS_MAX_HOLD_DAYS or (held == cfg.POS_MAX_HOLD_DAYS and close_ts.time() >= exit_t):
            realized += open_frac * leg(c)
            label = "TIME" if hit == 0 else f"TP{hit}+TIME"
            return _closed(label, realized, best, entry, risk, sign, c, ts)
    # still open: mark to the last bar
    last = float(after["close"].iloc[-1]) if len(after) else entry
    return {"status": "OPEN", "outcome": (f"TP{hit}" if hit else None),
            "r_multiple": round(realized + open_frac * leg(last), 3),
            "mfe_r": round((best - entry) * sign / risk, 3)}


def _closed(outcome, realized, best, entry, risk, sign, px, ts) -> Dict:
    return {"status": "CLOSED", "outcome": outcome, "r_multiple": round(realized, 3),
            "mfe_r": round((best - entry) * sign / risk, 3), "exit_price": round(float(px), 2),
            "exit_time": ts.to_pydatetime()}


def update_outcomes() -> int:
    """Score every open watch signal from stored candles. Returns how many closed."""
    closed = 0
    with session_scope() as db:
        open_rows = db.query(WatchSignal).filter(WatchSignal.status == "OPEN").all()
        work = [{c.name: getattr(r, c.name) for c in WatchSignal.__table__.columns}
                for r in open_rows]
    for sig in work:
        try:
            with session_scope() as db:
                rows = (db.query(Candle.timestamp, Candle.open, Candle.high, Candle.low,
                                 Candle.close)
                          .filter(Candle.symbol == sig["symbol"], Candle.timeframe == "5m",
                                  Candle.timestamp > sig["bar_time"])
                          .order_by(Candle.timestamp).all())
            bars = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
            upd = score(sig, bars)
            upd["checked_at"] = now_naive()
            with session_scope() as db:
                db.query(WatchSignal).filter(WatchSignal.id == sig["id"]).update(upd)
            closed += upd["status"] == "CLOSED"
        except Exception as exc:
            logger.warning("[Watch] Could not score %s #%s: %s", sig["symbol"], sig["id"], exc)
    return closed


def report(days: int = 60) -> Dict:
    since = now_naive() - timedelta(days=days)
    with session_scope() as db:
        rows = (db.query(WatchSignal).filter(WatchSignal.timestamp >= since)
                  .order_by(WatchSignal.timestamp.desc()).all())
        items = [{c.name: getattr(r, c.name) for c in WatchSignal.__table__.columns} for r in rows]
    closed = [r for r in items if r["status"] == "CLOSED" and r["r_multiple"] is not None]
    rs = [r["r_multiple"] for r in closed]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    summary = {
        "signals": len(items), "open": len(items) - len(closed), "closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "tp1_rate": round(sum(1 for r in closed if r["outcome"] not in ("SL", "TIME")) /
                          len(closed) * 100, 1) if closed else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2) if rs else 0.0,
        "pf": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
    }
    return {"summary": summary, "rows": items, "days": days}
