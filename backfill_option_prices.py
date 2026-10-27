"""
Fill option prices for signals recorded without them (wrong strike, or created
before option tracking existed), from Kite's 1-minute history.

    python backfill_option_prices.py --ids 60,61,62,63,65,67          # dry run
    python backfill_option_prices.py --ids 60,61,62,63,65,67 --apply  # write

For each signal:
  - contract: the signal's expiry and CE/PE, at the nearest LISTED round
    strike (same rule the live options layer now uses);
  - opt_entry: the option's 1-minute close at the signal time;
  - opt_sl / opt_tp1-3: Black-76 re-pricing at the stock levels with the IV
    solved from that entry premium (same method as live);
  - scale-outs: TP1/TP2 times are found from the futures' 1-minute closes
    after entry (the live manager checks the last price once a minute), and
    the option is booked at its close in that minute, 50%/30% of quantity;
  - closed trades: the rest is booked at the option close at exit_time;
    open trades are marked to the latest option close.
The result is a reconstruction from minute closes, not a fill record.
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from clock import now_naive
from config import cfg
from database import Signal, session_scope
from feed_kite import KiteFeed, _as_date
from options_layer import (black76_greeks, black76_price, implied_vol, nearest_listed_strike,
                           round_strike_step, years_to_expiry)

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s optfill  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("optfill")
CLOSED = ("TP3", "SL", "TRAIL", "SQUAREOFF")


def _contract(feed: KiteFeed, symbol: str, expiry: date, opt_type: str, strike: int) -> Optional[Dict]:
    root = symbol.upper()
    for exchange in ("NFO", "BFO"):
        for row in feed._instruments.get(exchange, []):
            if (str(row.get("instrument_type", "")).upper() == opt_type
                    and str(row.get("name", "")).upper() == root
                    and _as_date(row.get("expiry")) == expiry
                    and abs(float(row.get("strike") or 0) - strike) < 0.51):
                return row
    return None


def _minutes(feed: KiteFeed, token: int, start: datetime) -> pd.DataFrame:
    rows = feed.kite.historical_data(token, start - timedelta(minutes=10), now_naive(),
                                     "minute", oi=True)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.rename(columns={"date": "timestamp"}).sort_values("timestamp").reset_index(drop=True)


def _at(df: pd.DataFrame, ts, col: str = "close") -> Optional[float]:
    """Close of the last minute bar that started at or before ts."""
    sub = df[df["timestamp"] <= pd.Timestamp(ts)]
    return float(sub[col].iloc[-1]) if len(sub) else None


def _first_reach(fut: pd.DataFrame, after, level: float, long_side: bool):
    sub = fut[fut["timestamp"] > pd.Timestamp(after)]
    hit = sub[sub["close"] >= level] if long_side else sub[sub["close"] <= level]
    return hit["timestamp"].iloc[0] if len(hit) else None


async def fill_one(feed: KiteFeed, s: Dict) -> Dict:
    long_side = s["direction"] == "LONG"
    opt_type = "CE" if long_side else "PE"
    expiry = date.fromisoformat(str(s["option_expiry"])[:10])
    entry, ts = float(s["entry"]), pd.Timestamp(s["timestamp"])

    listed = await feed.get_strikes(s["symbol"], {"date": expiry}, opt_type)
    strike = nearest_listed_strike(entry, listed, round_strike_step(entry))
    if not strike:
        return {"error": "no listed strike"}
    row = _contract(feed, s["symbol"], expiry, opt_type, strike)
    if not row:
        return {"error": f"contract {strike} {opt_type} not found"}
    opt = _minutes(feed, int(row["instrument_token"]), ts.to_pydatetime())
    if opt.empty:
        return {"error": "no option candles"}

    prem = _at(opt, ts)
    if not prem:
        return {"error": "no premium at signal time"}
    t = years_to_expiry(expiry, now=ts.to_pydatetime())
    iv = implied_vol(prem, entry, strike, t, long_side)
    out = {"contract": row["tradingsymbol"], "atm_strike": int(strike), "option_type": opt_type,
           "option_hint": f"{s['symbol']} {strike} {opt_type} {expiry.isoformat()}",
           "option_ltp": round(prem, 2), "opt_entry": round(prem, 2)}
    oi_at = _at(opt, ts, "oi") if "oi" in opt else None
    if oi_at is not None:
        out["oi_at_entry"] = oi_at
        blocked = 0 < oi_at < cfg.MIN_OPTION_OI
        out["option_blocked"] = blocked
        out["option_block_reason"] = (f"Strike had {oi_at:,.0f} OI at entry, below "
                                      f"{cfg.MIN_OPTION_OI:,.0f}" if blocked else None)
    if iv:
        out["option_iv"] = round(iv, 4)
        out["option_delta"] = round(black76_greeks(entry, strike, t, iv, long_side)["delta"], 4)
        base = black76_price(entry, strike, t, iv, long_side)
        for key, lvl in (("opt_sl", "sl"), ("opt_tp1", "tp1"), ("opt_tp2", "tp2"), ("opt_tp3", "tp3")):
            out[key] = round(max(0.05, prem + black76_price(float(s[lvl]), strike, t, iv, long_side) - base), 2)

    # Follow the stock-level scale-outs
    fut = await feed.get_historical(s["symbol"], "1m", 2000, use_futures=True,
                                    from_date=ts.date())
    fut["timestamp"] = pd.to_datetime(fut["timestamp"])
    qty = int(s["qty"] or 0)
    q1, q2 = int(qty * cfg.SCALE_OUT[0]), int(qty * cfg.SCALE_OUT[1])
    realized, qty_open, events = 0.0, qty, []
    t1 = _first_reach(fut, ts, float(s["tp1"]), long_side) if s.get("tp1_hit") else None
    if t1 is not None:
        p = _at(opt, t1); realized += (p - prem) * q1; qty_open -= q1
        events.append(f"TP1 {t1:%d-%m %H:%M} @ {p:.2f}")
    t2 = _first_reach(fut, t1, float(s["tp2"]), long_side) if (s.get("tp2_hit") and t1 is not None) else None
    if t2 is not None:
        p = _at(opt, t2); realized += (p - prem) * q2; qty_open -= q2
        events.append(f"TP2 {t2:%d-%m %H:%M} @ {p:.2f}")
    if s["status"] in CLOSED and s.get("exit_time"):
        p = _at(opt, s["exit_time"])
        realized += (p - prem) * qty_open
        events.append(f"exit {pd.Timestamp(s['exit_time']):%d-%m %H:%M} @ {p:.2f}")
        out.update(opt_exit=round(p, 2), opt_ltp_now=round(p, 2), opt_qty_open=0,
                   opt_realized=round(realized, 2), opt_pnl=round(realized, 2))
    else:
        p = float(opt["close"].iloc[-1])
        events.append(f"now {opt['timestamp'].iloc[-1]:%d-%m %H:%M} @ {p:.2f}")
        out.update(opt_ltp_now=round(p, 2), opt_qty_open=qty_open, opt_realized=round(realized, 2),
                   opt_pnl=round(realized + (p - prem) * qty_open, 2))
    out["opt_updated_at"] = now_naive()
    out["_events"] = events
    return out


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    from backfill_history import start_health_server
    start_health_server()          # Railway's shared /health check

    feed = KiteFeed()
    await feed.connect()
    with session_scope() as db:
        rows = db.query(Signal).filter(Signal.id.in_(ids)).all()
        sigs = [{c.name: getattr(r, c.name) for c in Signal.__table__.columns} for r in rows]

    for s in sigs:
        try:
            upd = await fill_one(feed, s)
        except Exception as exc:
            upd = {"error": str(exc)}
        events = upd.pop("_events", [])
        logger.info("FILL #%s %s %s %s -> %s | %s", s["id"], s["symbol"], s["direction"],
                    s["status"], json.dumps({k: v for k, v in upd.items() if k != "opt_updated_at"},
                                            default=str), "; ".join(events))
        if args.apply and "error" not in upd:
            fields = {k: v for k, v in upd.items() if k in Signal.__table__.columns.keys()}
            with session_scope() as db:
                db.query(Signal).filter(Signal.id == s["id"]).update(fields)
    logger.info("OPTFILL DONE (%s)", "applied" if args.apply else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
