"""
P&L page data: filtered signal list, summary, and period series.

Two P&L bases:
  option - opt_pnl, the premium change x quantity of the suggested CE/PE
           (only signals recorded with option tracking have it);
  stock  - pnl, the stock/futures-level paper P&L the engine has always kept.
The two are never mixed in one total.

Closed trades are counted on the day they closed; open trades are shown
separately at their latest mark.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from clock import now_naive
from config import cfg
from database import Signal, session_scope

CLOSED = ("TP3", "SL", "TRAIL", "SQUAREOFF")
OPEN = ("OPEN", "RUNNING")


@dataclass
class PnlFilter:
    period: str = "month"            # today | week | month | all | custom
    date_from: Optional[str] = None  # YYYY-MM-DD, custom only
    date_to: Optional[str] = None
    group: str = "day"               # day | week | month
    symbol: str = ""
    asset: str = "all"               # all | INDEX | EQUITY
    side: str = "all"                # all | CALL | PUT
    result: str = "all"              # all | win | loss | open
    exit: str = "all"                # all | TP3 | SL | TRAIL | SQUAREOFF
    basis: str = "option"            # option | stock
    errors: List[str] = field(default_factory=list)

    def normalise(self) -> "PnlFilter":
        allowed = {
            "period": ("today", "week", "month", "all", "custom"),
            "group": ("day", "week", "month"),
            "asset": ("all", "INDEX", "EQUITY"),
            "side": ("all", "CALL", "PUT"),
            "result": ("all", "win", "loss", "open"),
            "exit": ("all",) + CLOSED,
            "basis": ("option", "stock"),
        }
        defaults = PnlFilter()
        for name, options in allowed.items():
            if getattr(self, name) not in options:
                setattr(self, name, getattr(defaults, name))
        self.symbol = (self.symbol or "").strip()
        return self

    def window(self, now: datetime = None):
        """(start, end) naive datetimes, either may be None."""
        now = now or now_naive()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.period == "today":
            return today, None
        if self.period == "week":
            return today - timedelta(days=today.weekday()), None
        if self.period == "month":
            return today.replace(day=1), None
        if self.period == "custom":
            start = end = None
            try:
                if self.date_from:
                    start = datetime.combine(date.fromisoformat(self.date_from), datetime.min.time())
                if self.date_to:
                    end = datetime.combine(date.fromisoformat(self.date_to), datetime.min.time()) \
                        + timedelta(days=1)
            except ValueError:
                self.errors.append("Dates must be YYYY-MM-DD")
            return start, end
        return None, None


def _pnl(sig: Signal, basis: str) -> Optional[float]:
    if basis == "option":
        return sig.opt_pnl if sig.opt_entry else None
    return sig.pnl or 0.0


def _bucket(d: date, group: str) -> str:
    if group == "month":
        return d.strftime("%Y-%m")
    if group == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    return d.isoformat()


def load(f: PnlFilter) -> List[Signal]:
    start, end = f.window()
    with session_scope() as db:
        q = db.query(Signal)
        # A trade belongs to the window if it was opened in it or closed in it.
        if start is not None:
            q = q.filter((Signal.timestamp >= start) | (Signal.exit_time >= start))
        if end is not None:
            q = q.filter(Signal.timestamp < end)
        syms = [s.strip().upper() for s in f.symbol.split(",") if s.strip()]
        if syms:
            q = q.filter(Signal.symbol.in_(syms))
        if f.asset != "all":
            q = q.filter(Signal.asset_class == f.asset)
        if f.side != "all":
            q = q.filter(Signal.direction == ("LONG" if f.side == "CALL" else "SHORT"))
        if f.exit != "all":
            q = q.filter(Signal.status == f.exit)
        rows = q.order_by(Signal.timestamp.desc()).all()

    if f.result == "open":
        rows = [r for r in rows if r.status in OPEN]
    elif f.result in ("win", "loss"):
        def ok(r):
            v = _pnl(r, f.basis)
            if r.status not in CLOSED or v is None:
                return False
            return v > 0 if f.result == "win" else v <= 0
        rows = [r for r in rows if ok(r)]
    return rows


def build(f: PnlFilter) -> Dict:
    f.normalise()
    rows = load(f)
    closed = [r for r in rows if r.status in CLOSED and _pnl(r, f.basis) is not None]
    opened = [r for r in rows if r.status in OPEN]
    vals = [_pnl(r, f.basis) for r in closed]
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v <= 0]
    net = sum(vals)
    gross_win, gross_loss = sum(wins), -sum(losses)

    # Period series on the close date, plus the running total.
    series: Dict[str, Dict] = {}
    for r in sorted(closed, key=lambda x: x.exit_time or x.timestamp):
        when = (r.exit_time or r.timestamp).date()
        b = series.setdefault(_bucket(when, f.group), {"pnl": 0.0, "trades": 0, "wins": 0})
        v = _pnl(r, f.basis)
        b["pnl"] += v
        b["trades"] += 1
        b["wins"] += 1 if v > 0 else 0
    labels = sorted(series)
    running, cum = 0.0, []
    for k in labels:
        running += series[k]["pnl"]
        cum.append(round(running, 2))
    # Drawdown trade by trade in close order, so it doesn't depend on grouping.
    eq, peak, max_dd = 0.0, 0.0, 0.0
    for r in sorted(closed, key=lambda x: x.exit_time or x.timestamp):
        eq += _pnl(r, f.basis)
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    open_mtm = [_pnl(r, f.basis) for r in opened if _pnl(r, f.basis) is not None]
    balance = cfg.ACCOUNT_BALANCE or 0
    r_vals = [r.r_multiple for r in closed if r.r_multiple is not None]
    no_option = len([r for r in rows if not r.opt_entry])

    summary = {
        "signals": len(rows), "closed": len(closed), "open": len(opened),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "net": round(net, 2),
        "net_pct": round(net / balance * 100, 2) if balance else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "best": round(max(vals), 2) if vals else None,
        "worst": round(min(vals), 2) if vals else None,
        "max_drawdown": round(max_dd, 2),
        "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
        "open_mtm": round(sum(open_mtm), 2) if open_mtm else 0.0,
        "account_balance": balance,
        "without_option_data": no_option,
    }
    chart = {
        "labels": labels,
        "pnl": [round(series[k]["pnl"], 2) for k in labels],
        "trades": [series[k]["trades"] for k in labels],
        "win_rate": [round(series[k]["wins"] / series[k]["trades"] * 100, 1) for k in labels],
        "cumulative": cum,
    }
    return {"filter": f, "summary": summary, "chart": chart,
            "rows": [row_view(r, f.basis) for r in rows]}


def option_now(r) -> Dict:
    """
    How to show the option's current price. Closed: the exit (sell) price and
    when it was taken - frozen after that, so it will differ from the live
    market. Open: the exchange last price, the bid/ask behind the P&L, the
    quote time, and whether that quote is stale.
    """
    from clock import now_naive
    from config import cfg
    g = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k, None))
    if g("status") in CLOSED:
        at = g("opt_exit_at") or g("exit_time")
        return {"price": g("opt_exit"), "closed": True,
                "note": f"exit (sold) {at:%d-%m %H:%M}" if at else "exit (sold)", "stale": False}
    upd = g("opt_updated_at")
    age = (now_naive() - upd).total_seconds() if upd else None
    bid, ask = g("opt_bid_now"), g("opt_ask_now")
    parts = []
    if bid or ask:
        parts.append(f"bid {bid or 0:,.2f} · ask {ask or 0:,.2f}")
    if upd:
        parts.append(f"at {upd:%H:%M:%S}")
    return {"price": g("opt_last_now") or g("opt_ltp_now"), "closed": False,
            "note": " · ".join(parts),
            "stale": bool(age is None or age > cfg.OPTION_QUOTE_STALE_SEC)}


def row_view(r: Signal, basis: str = "option") -> Dict:
    def fmt_t(v):
        return v.strftime("%d-%m %H:%M") if v else None
    return {
        "id": r.id, "time": fmt_t(r.timestamp), "exit_time": fmt_t(r.exit_time),
        "symbol": r.symbol, "asset": r.asset_class,
        "side": "CALL" if r.direction == "LONG" else "PUT",
        "contract": (f"{int(r.atm_strike)} {r.option_type or ''}".strip() if r.atm_strike else None),
        "expiry": r.option_expiry, "option_hint": r.option_hint,
        "lots": r.lots, "qty": r.qty,
        "opt_entry": r.opt_entry, "opt_sl": r.opt_sl, "opt_tp1": r.opt_tp1,
        "opt_tp2": r.opt_tp2, "opt_tp3": r.opt_tp3,
        "opt_now": r.opt_exit if r.status in CLOSED else r.opt_ltp_now,
        "opt_view": option_now(r) if r.opt_entry else None,
        "opt_pnl": r.opt_pnl if r.opt_entry else None,
        "entry": r.entry, "sl": r.sl, "tp1": r.tp1, "tp2": r.tp2, "tp3": r.tp3,
        "exit_price": r.exit_price,
        "stock_pnl": r.pnl, "r": r.r_multiple,
        "status": r.status, "tp1_hit": r.tp1_hit, "tp2_hit": r.tp2_hit,
        "notes": r.notes, "score": r.composite_score,
        "why": _why(r),
    }


def _why(r: Signal) -> Optional[str]:
    """Plain-words reason the trade closed (analytics outcome vocabulary)."""
    if r.status not in CLOSED:
        return None
    try:
        import analytics
        return analytics.OUTCOME_LABELS.get(analytics._classify(r))
    except Exception:
        return None
