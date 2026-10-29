"""
Trend-day entries for the indices - a study, nothing here touches the live
scanner.

The squeeze scanner only trades breakouts from compression, so trending index
days that never compress are not traded. This tests three entries that need
no squeeze, on stored 5-minute history (hist_candles):

  orb15 / orb30  Opening-range breakout. Range = first 15 / 30 minutes. Entry
                 on the first 5-min close beyond it (before 13:00), stop at
                 the other side of the range. One trade a day.
  pullback       15-min trend (EMA20 > EMA50, close beyond EMA20, ADX > 20).
                 A 5-min dip that touches the 15-min EMA20, then a 5-min close
                 back beyond the previous bar's high/low. Stop under the last
                 6 bars' swing. 09:45-14:30, at most 2 a day.
  stflip         15-min Supertrend(10,3) turns, ADX > 20. Entry at that bar's
                 close, stop at the Supertrend line. 09:45-14:30.

Exits copy the live plan: targets 1.5/2.5/4 R with 50/30/20 scale-out, stop
to entry at T1 and to T1 at T2, 15-min Supertrend trail and flip exit, and
either 15:20 the same day ("eod") or 15:20 on the 2nd trading day ("2d").
A bar touching both stop and target counts as the stop. 0.02% cost per side.

    python trend_study.py --symbols "BANKNIFTY,NIFTY 50,SENSEX" --days 400
"""
import argparse
import json
import logging
import sys
from datetime import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from indicators import adx, atr, ema, supertrend

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s trend  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("trend")

COST = 0.0002
TARGETS = (1.5, 2.5, 4.0)
SCALE = (0.5, 0.3, 0.2)
EXIT_T = time(15, 20)


# --------------------------------------------------------------------------- #
def to_15m(d5: pd.DataFrame) -> pd.DataFrame:
    """Session-aligned 15-minute bars from 5-minute bars (09:15, 09:30, ...)."""
    g = d5.set_index("timestamp")
    out = g.resample("15min", origin="start_day", offset="15min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    out = out.reset_index()
    out["end"] = out["timestamp"] + pd.Timedelta(minutes=15)
    return out


def add_15m_ind(q: pd.DataFrame) -> pd.DataFrame:
    q = q.copy()
    q["ema20"], q["ema50"] = ema(q["close"], 20), ema(q["close"], 50)
    q["adx"] = adx(q, 14)
    line, d = supertrend(q, 10, 3.0)
    q["st"], q["st_dir"] = line, d
    return q


def ctx_at(q: pd.DataFrame, ts) -> Optional[pd.Series]:
    """Last 15-minute bar completed by 5-minute bar close ts (bar start + 5m)."""
    ends = q.attrs.get("_ends")
    if ends is None:
        ends = q["end"].values
        q.attrs["_ends"] = ends
    pos = int(np.searchsorted(ends, np.datetime64(ts), side="right")) - 1
    return q.iloc[pos] if pos >= 0 else None


# --------------------------------------------------------------------------- #
def simulate(d5: pd.DataFrame, q: pd.DataFrame, i: int, side: int, entry: float, stop: float,
             mode: str, trail: bool = True) -> Optional[Dict]:
    """Walk 5-minute bars after entry bar i. Returns R and the exit reason."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tps = [entry + side * m * risk for m in TARGETS]
    entry_day = d5["timestamp"].iloc[i].date()
    days = d5.attrs.get("_days")
    if days is None:
        days = sorted(d5["timestamp"].dt.date.unique())
        d5.attrs["_days"] = days
    di = days.index(entry_day)
    last_day = days[min(len(days) - 1, di + (0 if mode == "eod" else 2))]
    realized, open_frac, hit, cur = 0.0, 1.0, 0, stop
    best = entry
    last_q_end = None

    def leg(px):
        return (px - entry) * side / risk

    TS = d5["timestamp"].tolist()
    O, H, L, C = (d5[c].values for c in ("open", "high", "low", "close"))
    for j in range(i + 1, len(d5)):
        ts = TS[j]
        if ts.date() > last_day:
            break
        o, h, l, c = O[j], H[j], L[j], C[j]
        best = max(best, h) if side > 0 else min(best, l)
        # stop first (gap through fills at the open)
        if (side > 0 and o <= cur) or (side < 0 and o >= cur):
            px = o
        elif (side > 0 and l <= cur) or (side < 0 and h >= cur):
            px = cur
        else:
            px = None
        if px is not None:
            realized += open_frac * leg(px)
            why = "SL" if hit == 0 and cur == stop else ("TRAIL-STOP" if hit == 0 else "TRAIL-PROFIT")
            return _done(realized, why, best, entry, risk, side)
        reach = (lambda p: h >= p) if side > 0 else (lambda p: l <= p)
        if hit == 0 and reach(tps[0]):
            realized += SCALE[0] * leg(tps[0]); open_frac -= SCALE[0]; hit = 1
            cur = entry if side * (entry - cur) > 0 else cur
        if hit == 1 and reach(tps[1]):
            realized += SCALE[1] * leg(tps[1]); open_frac -= SCALE[1]; hit = 2
            cur = tps[0] if side * (tps[0] - cur) > 0 else cur
        if hit == 2 and reach(tps[2]):
            realized += open_frac * leg(tps[2])
            return _done(realized, "TP3", best, entry, risk, side)
        # 15-minute Supertrend: trail and flip on completed bars
        bar_close = ts + pd.Timedelta(minutes=5)
        if trail:
            k = ctx_at(q, bar_close)
            if k is not None and k["end"] == bar_close and k["end"] != last_q_end:
                last_q_end = k["end"]
                if (side > 0 and k["st_dir"] == -1) or (side < 0 and k["st_dir"] == 1):
                    realized += open_frac * leg(c)
                    return _done(realized, "ST-FLIP", best, entry, risk, side)
                line = float(k["st"])
                if side * (line - cur) > 0:
                    cur = line
        if ts.date() == last_day and bar_close.time() >= EXIT_T:
            realized += open_frac * leg(c)
            return _done(realized, "TIME", best, entry, risk, side)
    return None                                    # ran out of data


def _done(realized, why, best, entry, risk, side) -> Dict:
    cost_r = 2 * COST * entry / risk
    return {"r": round(realized - cost_r, 3), "exit": why,
            "mfe": round((best - entry) * side / risk, 3)}


# --------------------------------------------------------------------------- #
def orb_entries(d5: pd.DataFrame, q: pd.DataFrame, minutes: int, adx_min: float = 0,
                with_gap: bool = False) -> List[Dict]:
    out = []
    n_or = minutes // 5
    prev_close = None
    for day, g in d5.groupby(d5["timestamp"].dt.date):
        idx = g.index.tolist()
        if len(idx) < n_or + 2:
            prev_close = g["close"].iloc[-1]
            continue
        orh = g["high"].iloc[:n_or].max()
        orl = g["low"].iloc[:n_or].min()
        day_open = g["open"].iloc[0]
        for k in idx[n_or:]:
            b = d5.loc[k]
            if b["timestamp"].time() >= time(13, 0):
                break
            side = 1 if b["close"] > orh else (-1 if b["close"] < orl else 0)
            if not side:
                continue
            entry = float(b["close"])
            stop = orl if side > 0 else orh
            risk_pct = abs(entry - stop) / entry * 100
            ok = 0.15 <= risk_pct <= 1.0
            c = ctx_at(q, b["timestamp"] + pd.Timedelta(minutes=5))
            if adx_min and (c is None or not c["adx"] >= adx_min):
                ok = False
            if with_gap and prev_close is not None and np.sign(day_open - prev_close) != side:
                ok = False
            if ok:
                out.append({"i": k, "side": side, "entry": entry, "stop": float(stop)})
            break                                   # first break only, taken or not
        prev_close = g["close"].iloc[-1]
    return out


def pullback_entries(d5: pd.DataFrame, q: pd.DataFrame) -> List[Dict]:
    out, per_day = [], {}
    a5 = atr(d5, 14)
    busy_until = -1
    for k in range(6, len(d5)):
        b = d5.iloc[k]
        t = b["timestamp"].time()
        if t < time(9, 45) or t > time(14, 30) or k <= busy_until:
            continue
        day = b["timestamp"].date()
        if per_day.get(day, 0) >= 2:
            continue
        c = ctx_at(q, b["timestamp"] + pd.Timedelta(minutes=5))
        if c is None or not c["adx"] > 20:
            continue
        up = c["ema20"] > c["ema50"] and c["close"] > c["ema20"]
        dn = c["ema20"] < c["ema50"] and c["close"] < c["ema20"]
        if not (up or dn):
            continue
        side = 1 if up else -1
        recent = d5.iloc[k - 3:k]
        touched = (recent["low"] <= c["ema20"]).any() if up else (recent["high"] >= c["ema20"]).any()
        prev = d5.iloc[k - 1]
        trig = b["close"] > prev["high"] if up else b["close"] < prev["low"]
        if not (touched and trig):
            continue
        swing = d5.iloc[k - 6:k + 1]
        pad = 0.05 * float(a5.iloc[k] or 0)
        stop = float(swing["low"].min() - pad) if up else float(swing["high"].max() + pad)
        entry = float(b["close"])
        risk_pct = abs(entry - stop) / entry * 100
        if not 0.1 <= risk_pct <= 1.0:
            continue
        out.append({"i": k, "side": side, "entry": entry, "stop": stop})
        per_day[day] = per_day.get(day, 0) + 1
        busy_until = k + 6                            # no re-entry for 30 min
    return out


def stflip_entries(d5: pd.DataFrame, q: pd.DataFrame) -> List[Dict]:
    out = []
    ends = {r["end"]: r for _, r in q.iterrows()}
    prev_dir = None
    qd = q.set_index("end")
    for k in range(len(d5)):
        b = d5.iloc[k]
        close_ts = b["timestamp"] + pd.Timedelta(minutes=5)
        if close_ts not in ends:
            continue
        r = ends[close_ts]
        pos = qd.index.get_loc(close_ts)
        if pos == 0:
            continue
        prev_dir = qd["st_dir"].iloc[pos - 1]
        t = b["timestamp"].time()
        if t < time(9, 45) or t > time(14, 30):
            continue
        if r["st_dir"] == prev_dir or not r["adx"] > 20:
            continue
        side = int(r["st_dir"])
        entry, stop = float(b["close"]), float(r["st"])
        risk_pct = abs(entry - stop) / entry * 100
        if side * (entry - stop) <= 0 or not 0.1 <= risk_pct <= 1.2:
            continue
        out.append({"i": k, "side": side, "entry": entry, "stop": stop})
    return out


# --------------------------------------------------------------------------- #
def run_symbol(d5: pd.DataFrame) -> Dict[str, List[Dict]]:
    d5 = d5.reset_index(drop=True)
    q = add_15m_ind(to_15m(d5))
    setups = {
        "orb15": orb_entries(d5, q, 15),
        "orb30": orb_entries(d5, q, 30),
        "orb15+adx20": orb_entries(d5, q, 15, adx_min=20),
        "orb30+gapdir": orb_entries(d5, q, 30, with_gap=True),
        "pullback": pullback_entries(d5, q),
        "stflip": stflip_entries(d5, q),
    }
    trades = {}
    for name, entries in setups.items():
        for mode in ("eod", "2d"):
            key = f"{name}/{mode}"
            trades[key] = []
            busy_until = -1
            for e in entries:
                if e["i"] <= busy_until:
                    continue                          # one position at a time
                res = simulate(d5, q, e["i"], e["side"], e["entry"], e["stop"], mode)
                if res is None:
                    continue
                res.update(time=str(d5["timestamp"].iloc[e["i"]]), side=e["side"])
                trades[key].append(res)
    return trades


def summary(rs: List[float]) -> Dict:
    a = np.asarray(rs, dtype=float)
    if not len(a):
        return {"n": 0}
    w, l = a[a > 0], a[a <= 0]
    eq = np.cumsum(a)
    dd = float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min())
    half = len(a) // 2
    return {"n": int(len(a)), "win%": round(len(w) / len(a) * 100, 1),
            "avgR": round(float(a.mean()), 3), "totR": round(float(a.sum()), 1),
            "PF": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None,
            "maxDD": round(dd, 1),
            "h1": round(float(a[:half].mean()), 3) if half else None,
            "h2": round(float(a[half:].mean()), 3) if half else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BANKNIFTY,NIFTY 50,SENSEX")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args(argv)
    try:
        from backfill_history import start_health_server
        start_health_server()
    except Exception:
        pass
    from database import engine
    from backtest import load_series
    allr: Dict[str, List[Dict]] = {}
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        d5 = load_series(engine, sym, "5m", args.days)
        if d5.empty:
            logger.warning("No 5m history for %s", sym)
            continue
        logger.info("%s: %d bars %s .. %s", sym, len(d5), d5["timestamp"].iloc[0], d5["timestamp"].iloc[-1])
        res = run_symbol(d5)
        for key, ts in res.items():
            s = summary([t["r"] for t in ts])
            ex = pd.Series([t["exit"] for t in ts]).value_counts().to_dict() if ts else {}
            logger.info("TREND %s | %-18s | %s | exits %s", sym, key, json.dumps(s), json.dumps(ex))
            allr.setdefault(key, []).extend(sorted(ts, key=lambda t: t["time"]))
    for key, ts in allr.items():
        ts = sorted(ts, key=lambda t: t["time"])
        logger.info("TREND ALL | %-18s | %s", key, json.dumps(summary([t["r"] for t in ts])))
    logger.info("TREND DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
