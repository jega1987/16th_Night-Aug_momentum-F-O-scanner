"""
Backtest of the live squeeze strategy on the stored history (hist_candles).

    python backtest.py                     # all symbols, current Railway settings
    python backtest.py --symbols "TCS,INFY" --days 120
    python backtest.py --label adx25 --set EQ_ADX_THRESHOLD=25

It replays 5-minute candles through the SAME code the live scanner uses
(FilterEngine.apply_all, SignalEngine._stop_distance, the positional exit
rules), so a result here describes the deployed logic, not a re-implementation.

How a signal is formed (hybrid mode, SQUEEZE_TIMEFRAME=15m):
  - completed 15-minute bars are built from the 5-minute ones; a squeeze is a
    run of at least MIN_SQUEEZE_BARS of them;
  - a CLOSED 5-minute bar closing outside the 15-minute squeeze range is a
    candidate; the full live filter stack decides;
  - entry = next 5-minute bar's open (the live scanner enters at the live price
    just after the bar closes); the live slip / back-inside checks apply.

How a trade is managed (positional mode):
  - stop = widest of the live stop candidates incl. 1.5 x hourly ATR; targets
    at TARGET_R_MULTS x stop; scale-out SCALE_OUT; stop to breakeven at TP1,
    to TP1 at TP2;
  - Supertrend(ST_PERIOD, ST_MULT) on 15-minute bars trails the stop and
    exits on a flip;
  - time stop at POS_EXIT_TIME on the POS_MAX_HOLD_DAYS-th trading day after
    entry;
  - inside one 5-minute bar, a stop touch is assumed to happen before a
    target touch (conservative); a gap through the stop fills at the open.
  - portfolio caps (MAX_SIGNALS_PER_DAY, MAX_OPEN_POSITIONS) applied in time
    order, highest composite first within a scan.

Known differences from live, reported in the output:
  - history is spot: OI is unknown (scored neutral 0.5) and index spot has
    no volume (the volume check is neutral for indices, as in live);
  - the option-expiry exit is not simulated (expiry dates of past contracts
    are not in the data);
  - results are in R (multiples of the initial stop), before option effects.
"""
import argparse
import json
import logging
import math
import os
import sys
import time as _time
import uuid
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s backtest  %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backtest")


def _apply_overrides(pairs: List[str]) -> Dict[str, str]:
    """--set KEY=VALUE pairs become environment variables before config loads."""
    out = {}
    for p in pairs or []:
        if "=" in p:
            k, v = p.split("=", 1)
            os.environ[k.strip()] = v.strip()
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
def load_series(engine, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    q = ("SELECT timestamp, open, high, low, close, volume, oi FROM hist_candles "
         "WHERE symbol = :s AND timeframe = :t AND timestamp >= :since ORDER BY timestamp")
    from sqlalchemy import text
    since = datetime.now() - timedelta(days=days + 5)
    with engine.connect() as conn:
        df = pd.read_sql(text(q), conn, params={"s": symbol, "t": timeframe, "since": since})
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df[(df["timestamp"].dt.time >= time(9, 15)) & (df["timestamp"].dt.time < time(15, 30))]
    return df.drop_duplicates("timestamp").reset_index(drop=True)


def list_symbols(engine) -> List[str]:
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT symbol FROM hist_candles WHERE timeframe = '5m' "
            "AND symbol NOT LIKE '%::FUT'")).fetchall()
    return sorted(r[0] for r in rows if r[0] != "INDIA VIX")


# --------------------------------------------------------------------------- #
class SymbolBacktest:
    """Signals and simulated trades for one symbol."""

    def __init__(self, symbol: str, d5: pd.DataFrame, cfg, filters, store,
                 eval_cache: Optional[Dict] = None, q15: Optional[pd.DataFrame] = None):
        from clock import _parse_time
        self.eval_cache = eval_cache if eval_cache is not None else {}
        self.symbol, self.cfg, self.filters, self.store = symbol, cfg, filters, store
        self.d5 = d5.reset_index(drop=True)
        self.q15 = q15 if q15 is not None else store.resample(self.d5, "15m")
        # A 15-minute bucket is complete at bucket start + 15 min.
        self.q15_done = (pd.to_datetime(self.q15["timestamp"]) + pd.Timedelta(minutes=15)).values
        self.bar_close = (pd.to_datetime(self.d5["timestamp"]) + pd.Timedelta(minutes=5)).values
        self.sig_start = _parse_time(cfg.SIGNAL_START_TIME, time(9, 45))
        self.sig_end = _parse_time(cfg.SIGNAL_END_TIME, time(14, 15))
        self.exit_t = _parse_time(cfg.POS_EXIT_TIME, time(15, 20))
        self.trades: List[Dict] = []
        self.rejects: Dict[str, int] = {}

    # ---------------- candidate pre-filter (vectorised) ---------------- #
    def _candidates(self) -> List[int]:
        """
        5-minute bars that close outside the range of a fresh coil, for every
        detector switched on by SQUEEZE_DETECTOR. Both detectors are causal
        (rolling windows over past bars only), so the full-series result at
        bar j equals what the live scanner computes from the bars up to j.
        """
        from indicators import compression_setups, squeeze_range
        cfg = self.cfg
        if len(self.q15) < cfg.BB_LENGTH + cfg.MIN_SQUEEZE_BARS + 5:
            return []
        dets = []
        for name, in_s, dur_s, fired_s in compression_setups(self.q15, cfg):
            in_sqz, dur, fired = in_s.values, dur_s.values, fired_s.values
            last_fire = -10 ** 9
            fresh = np.zeros(len(self.q15), dtype=bool)
            for j in range(len(self.q15)):
                if fired[j]:
                    last_fire = j
                live = in_sqz[j] and dur[j] >= cfg.MIN_SQUEEZE_BARS
                fresh[j] = live or (j - last_fire) <= cfg.MAX_BARS_SINCE_FIRE
            dets.append((name, fresh, in_s.reset_index(drop=True), dur_s.reset_index(drop=True)))
        q = self.q15.reset_index(drop=True)

        # Last completed 15-minute bar for every 5-minute bar close.
        jidx = np.searchsorted(self.q15_done, self.bar_close, side="right") - 1
        self._jidx = jidx
        rng = {}
        out = []
        closes = self.d5["close"].values
        stamps = pd.to_datetime(self.d5["timestamp"])
        for i in range(len(self.d5) - 1):
            j = jidx[i]
            if j < 0:
                continue
            close_t = (stamps.iloc[i] + pd.Timedelta(minutes=5)).time()
            if not (self.sig_start <= close_t <= self.sig_end):
                continue
            for name, fresh, in_s, dur_s in dets:
                if not fresh[j]:
                    continue
                key = (name, j)
                if key not in rng:
                    rng[key] = squeeze_range(q.iloc[:j + 1], in_s.iloc[:j + 1], dur_s.iloc[:j + 1],
                                             cfg.SQUEEZE_RANGE_MAX_BARS)
                hi, lo = rng[key]
                if closes[i] > hi or closes[i] < lo:
                    out.append(i)
                    break
        return out

    # ---------------- opening-volume baseline ---------------- #
    def _baseline(self, i: int) -> Optional[float]:
        cfg = self.cfg
        if not cfg.OPEN_VOLUME_BASELINE:
            return None
        ts = pd.Timestamp(self.d5["timestamp"].iloc[i])
        hh, mm = (int(x) for x in cfg.OPEN_BASELINE_UNTIL.split(":")[:2])
        if (ts.hour, ts.minute) >= (hh, mm):
            return None
        hist = self.d5.iloc[max(0, i - 75 * (cfg.OPEN_BASELINE_DAYS + 2)):i]
        st = pd.to_datetime(hist["timestamp"])
        same = hist[(st.dt.hour == ts.hour) & (st.dt.minute == ts.minute)
                    & (st.dt.normalize() < ts.normalize())]
        vols = pd.to_numeric(same["volume"], errors="coerce").dropna()
        vols = vols[vols > 0].tail(cfg.OPEN_BASELINE_DAYS)
        return float(vols.mean()) if len(vols) >= cfg.OPEN_BASELINE_MIN_DAYS else None

    # ---------------- full evaluation of one candidate ---------------- #
    def _filter_signature(self):
        c = self.cfg
        return (c.volume_mult(self.symbol), c.adx_threshold(self.symbol), c.min_composite(self.symbol),
                c.RSI_EXHAUSTION_FILTER, c.GAP_FILTER, c.OPEN_VOLUME_BASELINE,
                c.TRADING_MODE, getattr(c, "SQUEEZE_DETECTOR", "ttm"),
                getattr(c, "BOX_MAX_WIDTH_ATR", None), getattr(c, "BOX_ATR_BARS", None))

    def _evaluate(self, i: int) -> Optional[Dict]:
        key = (i, self._filter_signature())
        if key in self.eval_cache:
            hit = self.eval_cache[key]
            return dict(hit) if hit else None
        out = self._evaluate_uncached(i)
        self.eval_cache[key] = dict(out) if out else None
        return out

    def _evaluate_uncached(self, i: int) -> Optional[Dict]:
        from indicators import atr
        from signal_engine import SignalEngine
        cfg = self.cfg
        window = self.d5.iloc[max(0, i - cfg.SQUEEZE_HISTORY_BARS + 1):i + 1].reset_index(drop=True)
        j = self._jidx[i]
        sqz_df = self.q15.iloc[max(0, j - 199):j + 1].reset_index(drop=True)
        # The resamples don't depend on the variant being tested - share them
        # across variants through the per-symbol cache.
        ctx = self.eval_cache.get(("ctx", i))
        if ctx is None:
            ctx = {"htf": self.store.resample(window, cfg.HTF_TIMEFRAME),   # live: includes forming bar
                   "hourly": self.store.resample(window, cfg.POS_SL_ATR_TIMEFRAME)}
            self.eval_cache[("ctx", i)] = ctx
        htf = ctx["htf"]
        res = self.filters.apply_all(window, htf=htf, oi={"change_pct": 0.0},
                                     symbol=self.symbol, sqz_df=sqz_df,
                                     vol_baseline=self._baseline(i))
        if not res.passed:
            import re
            key = re.sub(r"[-+]?\d[\d.,]*", "#", (res.reason or "rejected")).split(" (")[0][:48]
            self.rejects[key] = self.rejects.get(key, 0) + 1
            return None

        atr_val = float(atr(window, cfg.ATR_LENGTH).iloc[-1])
        if atr_val <= 0:
            return None
        atr_htf = float(atr(htf, cfg.ATR_LENGTH).iloc[-1]) if len(htf) >= cfg.ATR_LENGTH + 2 else None
        hourly = ctx["hourly"]
        atr_pos = float(atr(hourly, cfg.ATR_LENGTH).iloc[-1]) if len(hourly) >= cfg.ATR_LENGTH + 2 else None

        bar_close = float(window["close"].iloc[-1])
        entry = float(self.d5["open"].iloc[i + 1])
        if abs(entry - bar_close) > cfg.MAX_ENTRY_SLIP_ATR * atr_val:
            self.rejects["entry slipped > ATR"] = self.rejects.get("entry slipped > ATR", 0) + 1
            return None
        hi, lo = res.meta.get("squeeze_high"), res.meta.get("squeeze_low")
        if (res.direction == "LONG" and entry <= hi) or (res.direction == "SHORT" and entry >= lo):
            self.rejects["entry back inside range"] = self.rejects.get("entry back inside range", 0) + 1
            return None

        setup = {"squeeze_high": hi, "squeeze_low": lo, "atr_htf": atr_htf, "atr_pos": atr_pos}
        dist, basis = SignalEngine._stop_distance(setup, entry, atr_val, self.symbol)
        if dist <= 0 or dist / entry * 100 > cfg.max_sl_pct(self.symbol):
            self.rejects["stop wider than cap"] = self.rejects.get("stop wider than cap", 0) + 1
            return None

        m = res.meta
        return {
            "symbol": self.symbol, "asset": m.get("asset_class"), "direction": res.direction,
            "signal_bar": str(self.d5["timestamp"].iloc[i]), "i": i,
            "entry_time": str(self.d5["timestamp"].iloc[i + 1]), "entry": entry,
            "risk": dist, "sl_basis": basis.split(" (")[0], "risk_pct": dist / entry * 100,
            "setup": m.get("setup", "ttm"),
            "composite": res.scores.get("composite"), "adx": m.get("adx_value"),
            "rsi": m.get("rsi_value"), "vol_ratio": m.get("vol_ratio"),
            "squeeze_bars": m.get("squeeze_bars"), "in_squeeze": m.get("in_squeeze"),
            "squeeze_start": m.get("squeeze_start"),
            "squeeze_width_pct": (hi - lo) / entry * 100 if hi and lo else None,
            "multi_day": (m.get("squeeze_start") or "")[:5] != pd.Timestamp(
                self.d5["timestamp"].iloc[i]).strftime("%d-%m"),
            "structure": res.scores.get("structure"), "sweep": res.scores.get("sweep"),
            "htf": res.scores.get("htf"), "hour": pd.Timestamp(self.d5["timestamp"].iloc[i]).hour,
        }

    # ---------------- trade management ---------------- #
    def _simulate(self, t: Dict) -> Dict:
        from clock import MarketClock
        from indicators import supertrend
        cfg = self.cfg
        long_side = t["direction"] == "LONG"
        sign = 1 if long_side else -1
        entry, risk = t["entry"], t["risk"]
        r1, r2, r3 = cfg.TARGET_R_MULTS
        tp1, tp2, tp3 = (entry + sign * r * risk for r in (r1, r2, r3))
        f1, f2, f3 = cfg.SCALE_OUT
        stop = entry - sign * risk
        realized, open_frac = 0.0, 1.0
        tp1_hit = tp2_hit = False
        best = entry
        entry_day = pd.Timestamp(t["entry_time"]).date()

        # Supertrend on completed 15-minute bars (causal).
        st_line, st_dir = (None, None)
        if cfg.USE_SUPERTREND_EXIT and len(self.q15) > cfg.ST_PERIOD + 2:
            line, d = supertrend(self.q15, cfg.ST_PERIOD, cfg.ST_MULT)
            st_line, st_dir = line.values, d.values

        def leg(price):
            return (price - entry) * sign / risk

        exit_price, reason, k_exit = None, None, None
        n = len(self.d5)
        for k in range(t["i"] + 1, n):
            bar = self.d5.iloc[k]
            o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            best = max(best, h) if long_side else min(best, l)

            # 1. stop (gap through it fills at the open)
            if (long_side and o <= stop) or (not long_side and o >= stop):
                exit_price, reason, k_exit = o, ("gap through stop"), k
                break
            if (long_side and l <= stop) or (not long_side and h >= stop):
                exit_price = stop
                reason = "trail" if (tp1_hit or abs(stop - (entry - sign * risk)) > 1e-9) else "stop"
                k_exit = k
                break

            # 2. targets
            reach = (lambda p: h >= p) if long_side else (lambda p: l <= p)
            if not tp1_hit and reach(tp1):
                realized += f1 * leg(tp1); open_frac -= f1; tp1_hit = True
                if cfg.MOVE_SL_TO_BE_AFTER_TP1:
                    stop = entry if not long_side and entry < stop or long_side and entry > stop else stop
            if tp1_hit and not tp2_hit and reach(tp2):
                realized += f2 * leg(tp2); open_frac -= f2; tp2_hit = True
                stop = max(stop, tp1) if long_side else min(stop, tp1)
            if reach(tp3):
                exit_price, reason, k_exit = tp3, "tp3", k
                break

            # 3. at a 15-minute bar completion: trail / flip
            close_ts = pd.Timestamp(bar["timestamp"]) + pd.Timedelta(minutes=5)
            trail_ok = not getattr(cfg, "BT_TRAIL_AFTER_TP1", False) or tp1_hit
            if st_dir is not None and trail_ok:
                j = self._jidx[k]
                if j >= 0 and pd.Timestamp(self.q15_done[j]) == close_ts:
                    sd, sl_ = int(st_dir[j]), float(st_line[j])
                    if (long_side and sd == -1) or (not long_side and sd == 1):
                        exit_price, reason, k_exit = c, "supertrend flip", k
                        break
                    stop = max(stop, sl_) if long_side else min(stop, sl_)

            # 4. time stop (and the experimental same-day exit)
            held = MarketClock.trading_days_between(entry_day, close_ts.date())
            at_bell = close_ts.time() >= self.exit_t
            if getattr(cfg, "BT_EXIT_EOD", False) and at_bell:
                exit_price, reason, k_exit = c, "eod square-off", k
                break
            if held > cfg.POS_MAX_HOLD_DAYS or (held == cfg.POS_MAX_HOLD_DAYS and at_bell):
                exit_price, reason, k_exit = c, "time stop", k
                break

        if exit_price is None:
            k_exit = n - 1
            exit_price, reason = float(self.d5["close"].iloc[-1]), "end of data"

        realized += open_frac * leg(exit_price)
        cost_r = 2 * cfg.COST_PCT_PER_SIDE * entry / risk
        t.update({
            "exit_time": str(self.d5["timestamp"].iloc[k_exit]), "exit_price": exit_price,
            "exit_reason": reason, "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
            "r": realized - cost_r, "mfe_r": leg(best), "bars_held": k_exit - t["i"],
        })
        return t

    def run(self) -> List[Dict]:
        cands = self._candidates()
        busy_until = -1
        last_signal = -10 ** 9
        cooldown = self.cfg.COOLDOWN_BARS
        for i in cands:
            if i <= busy_until or i - last_signal < cooldown:
                continue
            t = self._evaluate(i)
            if not t:
                continue
            t = self._simulate(t)
            self.trades.append(t)
            last_signal = i
            busy_until = i + t["bars_held"]
        return self.trades


# --------------------------------------------------------------------------- #
def apply_portfolio_caps(trades: List[Dict], max_per_day: int, max_open: int) -> List[Dict]:
    """Time order; within one scan, highest composite first."""
    trades = sorted(trades, key=lambda t: (t["signal_bar"], -(t["composite"] or 0)))
    taken, open_exits, per_day = [], [], {}
    for t in trades:
        now = t["entry_time"]
        open_exits = [e for e in open_exits if e > now]
        day = now[:10]
        if max_per_day and per_day.get(day, 0) >= max_per_day:
            continue
        if max_open and len(open_exits) >= max_open:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        open_exits.append(t["exit_time"])
        taken.append(t)
    return taken


def stats(rs: List[float]) -> Dict:
    if not rs:
        return {"n": 0}
    a = np.array(rs, dtype=float)
    wins, losses = a[a > 0], a[a <= 0]
    eq = np.cumsum(a)
    dd = float((eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min()) if len(a) else 0.0
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
    return {"n": int(len(a)), "win%": round(len(wins) / len(a) * 100, 1),
            "avgR": round(float(a.mean()), 3), "totalR": round(float(a.sum()), 1),
            "PF": round(pf, 2), "avgWinR": round(float(wins.mean()), 2) if len(wins) else 0,
            "avgLossR": round(float(losses.mean()), 2) if len(losses) else 0,
            "maxDD_R": round(dd, 1)}


def breakdown(df: pd.DataFrame, col: str, bins=None, labels=None) -> Dict:
    d = df.copy()
    if bins is not None:
        d[col] = pd.cut(d[col], bins=bins, labels=labels, include_lowest=True)
    out = {}
    for k, g in d.groupby(col, observed=True):
        out[str(k)] = stats(g["r"].tolist())
    return out


def report(trades: List[Dict], label: str, notes: Dict) -> Dict:
    df = pd.DataFrame(trades)
    rep = {"label": label, "notes": notes, "overall": stats(df["r"].tolist() if len(df) else [])}
    if df.empty:
        return rep
    df["month"] = df["entry_time"].str[:7]
    half = sorted(df["entry_time"])[len(df) // 2]
    df["half"] = np.where(df["entry_time"] < half, "1st half", "2nd half")
    rep.update({
        "by_month": breakdown(df, "month"),
        "by_half": breakdown(df, "half"),
        "by_direction": breakdown(df, "direction"),
        "by_setup": breakdown(df, "setup") if "setup" in df else None,
        "by_asset": breakdown(df, "asset"),
        "by_exit": breakdown(df, "exit_reason"),
        "by_adx": breakdown(df, "adx", [0, 25, 30, 40, 100], ["<25", "25-30", "30-40", "40+"]),
        "by_rsi": breakdown(df, "rsi", [0, 25, 35, 45, 55, 65, 75, 100],
                            ["<25", "25-35", "35-45", "45-55", "55-65", "65-75", "75+"]),
        "by_vol_ratio": breakdown(df, "vol_ratio", [0, 2, 3, 5, 1000], ["<2", "2-3", "3-5", "5+"]),
        "by_squeeze_bars": breakdown(df, "squeeze_bars", [0, 8, 12, 20, 1000],
                                     ["6-8", "9-12", "13-20", "21+"]),
        "by_still_in_squeeze": breakdown(df, "in_squeeze"),
        "by_multi_day": breakdown(df, "multi_day"),
        "by_hour": breakdown(df, "hour"),
        "by_composite": breakdown(df, "composite", [0, 0.75, 0.85, 0.95, 1.01],
                                  ["<0.75", "0.75-0.85", "0.85-0.95", "0.95+"]),
        "by_sl_basis": breakdown(df, "sl_basis"),
        "tp1_hit%": round(df["tp1_hit"].mean() * 100, 1),
        "median_bars_held": int(df["bars_held"].median()),
        "avg_mfe_R": round(float(df["mfe_r"].mean()), 2),
    })
    return rep


def save_trades(engine, trades: List[Dict], run_id: str, label: str) -> None:
    if not trades:
        return
    df = pd.DataFrame(trades).drop(columns=["i"], errors="ignore")
    df.insert(0, "run_id", run_id)
    df.insert(1, "label", label)
    df["created_at"] = datetime.utcnow()
    # New result fields (e.g. "setup") need their column on an existing table.
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if insp.has_table("backtest_trades"):
        have = {c["name"] for c in insp.get_columns("backtest_trades")}
        with engine.begin() as conn:
            for col in df.columns:
                if col not in have:
                    kind = "DOUBLE PRECISION" if pd.api.types.is_float_dtype(df[col]) else "TEXT"
                    if pd.api.types.is_bool_dtype(df[col]):
                        kind = "BOOLEAN"
                    conn.execute(text(f'ALTER TABLE backtest_trades ADD COLUMN "{col}" {kind}'))
    df.to_sql("backtest_trades", engine, if_exists="append", index=False, chunksize=2000)


# --------------------------------------------------------------------------- #
# Variant sets: (name, {config attribute: value}). Values are applied to the
# loaded config for that variant only. BT_* flags exist only in the backtest.
PRESETS = {
    "base": [("live-rules", {})],
    # 26-Oct ATHERENERG: a flat afternoon coil after a trending morning that
    # Bollinger-inside-Keltner never registered. Does a tight-range box setup
    # add winners, or just more signals?
    "box": [
        ("live-rules", {}),
        ("either-1.5", {"SQUEEZE_DETECTOR": "either", "BOX_MAX_WIDTH_ATR": 1.5}),
        ("either-2.0", {"SQUEEZE_DETECTOR": "either", "BOX_MAX_WIDTH_ATR": 2.0}),
        ("either-2.5", {"SQUEEZE_DETECTOR": "either", "BOX_MAX_WIDTH_ATR": 2.5}),
        ("live-no-rsi-cap", {"RSI_EXHAUSTION_FILTER": False}),
        ("either-2.0-no-rsi-cap", {"SQUEEZE_DETECTOR": "either", "BOX_MAX_WIDTH_ATR": 2.0,
                                   "RSI_EXHAUSTION_FILTER": False}),
    ],
    "oct25": [
        ("live-rules", {}),
        ("eod-exit", {"BT_EXIT_EOD": True}),
        ("targets-1R-2R-3R", {"TARGET_R_MULTS": [1.0, 2.0, 3.0]}),
        ("start-0945", {"SIGNAL_START_TIME": "09:45"}),
        ("stock-volume-3x", {"EQ_VOLUME_MULT": 3.0}),
        ("trail-after-tp1", {"BT_TRAIL_AFTER_TP1": True}),
        ("combo-eod+1R+0945+trailTP1", {"BT_EXIT_EOD": True, "TARGET_R_MULTS": [1.0, 2.0, 3.0],
                                         "SIGNAL_START_TIME": "09:45", "BT_TRAIL_AFTER_TP1": True}),
    ],
}


def _set_cfg(cfg, changes: Dict) -> Dict:
    """Apply attribute changes; returns the previous values for restoring."""
    saved = {}
    for k, v in changes.items():
        saved[k] = getattr(cfg, k, None)
        setattr(cfg, k, v)
    return saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest the live squeeze strategy")
    ap.add_argument("--days", type=int, default=180, help="5-minute history to replay")
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--label", type=str, default="live-rules")
    ap.add_argument("--set", action="append", default=[], help="KEY=VALUE config override")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--preset", type=str, default="base", help="variant set, e.g. oct25")
    args = ap.parse_args(argv)

    overrides = _apply_overrides(args.set)
    from backfill_history import start_health_server
    start_health_server()          # Railway's shared /health check
    from config import cfg
    from candle_store import CandleStore
    from database import engine
    from filters import FilterEngine

    if cfg.SQUEEZE_TIMEFRAME != "15m":
        logger.warning("SQUEEZE_TIMEFRAME=%s - this backtest models the 15m hybrid mode",
                       cfg.SQUEEZE_TIMEFRAME)
    filters = FilterEngine(cfg)
    store = CandleStore.__new__(CandleStore)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or list_symbols(engine)
    logger.info("Backtest '%s': %d symbols, %d days | mode=%s squeeze=%s ADX eq>%s idx>%s "
                "| overrides=%s", args.label, len(symbols), args.days, cfg.TRADING_MODE,
                cfg.SQUEEZE_TIMEFRAME, cfg.EQ_ADX_THRESHOLD, cfg.ADX_THRESHOLD, overrides)

    variants = PRESETS.get(args.preset, PRESETS["base"])
    if args.preset != "base":
        logger.info("Preset '%s': %s", args.preset, ", ".join(v[0] for v in variants))

    t0 = _time.time()
    per_variant: Dict[str, List[Dict]] = {v[0]: [] for v in variants}
    rejects: Dict[str, int] = {}
    for n, sym in enumerate(symbols, 1):
        try:
            d5 = load_series(engine, sym, "5m", args.days)
            if len(d5) < 400:
                continue
            cache: Dict = {}
            q15 = store.resample(d5, "15m")
            for name, changes in variants:
                saved = _set_cfg(cfg, changes)
                try:
                    bt = SymbolBacktest(sym, d5, cfg, filters, store, cache, q15)
                    per_variant[name] += bt.run()
                    if name == variants[0][0]:
                        for k, v in bt.rejects.items():
                            rejects[k] = rejects.get(k, 0) + v
                finally:
                    _set_cfg(cfg, saved)
        except Exception as exc:
            logger.warning("%s failed: %s", sym, exc)
        if n % 20 == 0 or n == len(symbols):
            logger.info("Progress %d/%d | %s | %.1f min", n, len(symbols),
                        {k: len(v) for k, v in per_variant.items()}, (_time.time() - t0) / 60)

    if len(variants) > 1:
        comparison = {}
        for name, changes in variants:
            saved = _set_cfg(cfg, changes)
            try:
                capped = apply_portfolio_caps(per_variant[name], cfg.MAX_SIGNALS_PER_DAY,
                                              cfg.MAX_OPEN_POSITIONS)
            finally:
                _set_cfg(cfg, saved)
            r = report(capped, name, {})
            eq = [t for t in capped if t.get("asset") == "EQUITY"]
            comparison[name] = {"changes": {k: str(v) for k, v in changes.items()},
                                "all": r["overall"], "stocks_only": stats([t["r"] for t in eq]),
                                "by_half": r.get("by_half"), "by_exit": r.get("by_exit"),
                                "by_setup": r.get("by_setup"),
                                "by_direction": r.get("by_direction"),
                                "tp1_hit%": r.get("tp1_hit%")}
            if not args.no_save:
                try:
                    save_trades(engine, capped, "cmp" + uuid.uuid4().hex[:6], args.preset + ":" + name)
                except Exception as exc:
                    logger.warning("Could not save %s: %s", name, exc)
        for name, val in comparison.items():
            logger.info("VARIANT %s %s", name, json.dumps(val, default=str))
        logger.info("TOP REJECTIONS (base) %s",
                    json.dumps(dict(sorted(rejects.items(), key=lambda x: -x[1])[:12])))
        logger.info("BACKTEST DONE preset=%s minutes=%.1f", args.preset, (_time.time() - t0) / 60)
        return 0

    all_trades = per_variant[variants[0][0]]

    capped = apply_portfolio_caps(all_trades, cfg.MAX_SIGNALS_PER_DAY, cfg.MAX_OPEN_POSITIONS)
    notes = {"days": args.days, "symbols": len(symbols), "overrides": overrides,
             "max_signals_per_day": cfg.MAX_SIGNALS_PER_DAY,
             "max_open_positions": cfg.MAX_OPEN_POSITIONS,
             "oi": "unknown in spot history - scored neutral 0.5",
             "index_volume": "none in spot history - volume check neutral for indices",
             "expiry_exit": "not simulated", "intrabar": "stop assumed before target",
             "minutes": round((_time.time() - t0) / 60, 1),
             "top_rejections": dict(sorted(rejects.items(), key=lambda x: -x[1])[:12])}
    run_id = uuid.uuid4().hex[:8]
    rep_all = report(all_trades, args.label + " (no portfolio caps)", {})
    rep = report(capped, args.label, notes)
    rep["run_id"] = run_id
    rep["uncapped_overall"] = rep_all["overall"]
    rep["uncapped_by_asset"] = rep_all.get("by_asset")

    if not args.no_save:
        try:
            save_trades(engine, capped, run_id, args.label)
            save_trades(engine, all_trades, run_id, args.label + "-uncapped")
        except Exception as exc:
            logger.warning("Could not save trades: %s", exc)

    for key, val in rep.items():
        logger.info("RESULT %s %s", key, json.dumps(val, default=str))
    logger.info("BACKTEST DONE run_id=%s", run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
