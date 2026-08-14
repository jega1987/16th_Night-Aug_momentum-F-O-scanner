"""
Performance analytics over the signal ledger.

Two of these matter more than the rest:

* `outcome_mix()` measures how often each exit actually happens. Expectancy for
  this strategy depends entirely on that distribution, and until it's measured
  any expectancy number is a guess.
* `factor_postmortem()` compares each filter's average score on winners against
  losers. A factor that scores the same on both isn't discriminating - it's
  costing you signals for nothing. A factor that scores *higher* on losers is
  actively harmful.

Everything here reads the database. Nothing here places or modifies a trade.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from clock import now_naive
from config import cfg
from database import Signal, session_scope

logger = logging.getLogger(__name__)

CLOSED_STATES = ("TP3", "SL", "TRAIL", "SQUAREOFF")
OPEN_STATES = ("OPEN", "RUNNING")

# How each exit maps to the outcome vocabulary used when reasoning about
# expectancy. TRAIL covers both "stopped at breakeven after TP1" and
# "Supertrend flipped", so the tp1/tp2 flags disambiguate.
OUTCOME_LABELS = {
    "stop": "Stopped out before T1",
    "breakeven": "T1 banked, then stopped near breakeven",
    "partial": "T2 banked, then trailed out",
    "runner": "Full run to T3",
    "squareoff": "Closed at the 15:20 bell",
}


def _classify(sig: Signal) -> str:
    if sig.status == "TP3":
        return "runner"
    if sig.status == "SQUAREOFF":
        return "squareoff"
    if sig.status == "SL" and not sig.tp1_hit:
        return "stop"
    if sig.tp2_hit:
        return "partial"
    if sig.tp1_hit:
        return "breakeven"
    return "stop"


def _load(days: int = None, timeframe: str = None, symbol: str = None) -> List[Signal]:
    with session_scope() as db:
        q = db.query(Signal)
        if timeframe:
            q = q.filter(Signal.timeframe == timeframe)
        if symbol:
            q = q.filter(Signal.symbol == symbol)
        if days:
            q = q.filter(Signal.timestamp >= now_naive() - timedelta(days=days))
        return q.order_by(Signal.timestamp).all()


# --------------------------------------------------------------------------- #
def headline(days: int = None, timeframe: str = None) -> Dict:
    rows = _load(days, timeframe)
    closed = [r for r in rows if r.status in CLOSED_STATES]
    wins = [r for r in closed if (r.pnl or 0) > 0]
    losses = [r for r in closed if (r.pnl or 0) < 0]

    gross_win = sum(r.pnl for r in wins) or 0.0
    gross_loss = abs(sum(r.pnl for r in losses)) or 0.0
    r_values = [r.r_multiple for r in closed if r.r_multiple is not None]

    equity, peak, max_dd, running = [], 0.0, 0.0, 0.0
    for r in closed:
        running += r.pnl or 0
        equity.append(running)
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    return {
        "signals": len(rows),
        "open": len([r for r in rows if r.status in OPEN_STATES]),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "net_pnl": round(sum(r.pnl or 0 for r in closed), 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        # Profit factor below 1.0 means the strategy is losing money.
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "best": round(max((r.pnl or 0 for r in closed), default=0), 2),
        "worst": round(min((r.pnl or 0 for r in closed), default=0), 2),
        "max_drawdown": round(max_dd, 2),
        "sample_warning": len(closed) < 30,
    }


def outcome_mix(days: int = None, timeframe: str = None) -> List[Dict]:
    """
    The measured distribution of exits. This is the number that decides whether
    the target structure works - everything else is downstream of it.
    """
    closed = [r for r in _load(days, timeframe) if r.status in CLOSED_STATES]
    buckets: Dict[str, List[Signal]] = {}
    for r in closed:
        buckets.setdefault(_classify(r), []).append(r)

    out = []
    for key, label in OUTCOME_LABELS.items():
        rows = buckets.get(key, [])
        r_vals = [x.r_multiple for x in rows if x.r_multiple is not None]
        out.append({
            "key": key,
            "label": label,
            "count": len(rows),
            "share_pct": round(len(rows) / len(closed) * 100, 1) if closed else 0.0,
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
            "pnl": round(sum(x.pnl or 0 for x in rows), 2),
        })
    return out


def factor_postmortem(days: int = None, timeframe: str = None) -> List[Dict]:
    """
    Average score per factor among winners vs losers.

    `edge` is the gap. Near zero means the factor isn't separating good trades
    from bad ones. Negative means it scores higher on losers, which is worse
    than useless.
    """
    closed = [r for r in _load(days, timeframe) if r.status in CLOSED_STATES]
    winners = [r for r in closed if (r.pnl or 0) > 0]
    losers = [r for r in closed if (r.pnl or 0) <= 0]

    factors = ["direction", "squeeze", "volume", "adx", "rsi",
               "structure", "sweep", "oi", "htf"]
    out = []
    for name in factors:
        def avg(rows):
            vals = []
            for r in rows:
                fb = r.factor_breakdown or {}
                v = fb.get(name)
                if isinstance(v, (int, float)):
                    vals.append(float(v))
            return sum(vals) / len(vals) if vals else None

        w, l = avg(winners), avg(losers)
        out.append({
            "factor": name,
            "avg_winners": round(w, 3) if w is not None else None,
            "avg_losers": round(l, 3) if l is not None else None,
            "edge": round(w - l, 3) if (w is not None and l is not None) else None,
            "samples": len([r for r in closed if isinstance((r.factor_breakdown or {}).get(name), (int, float))]),
        })
    out.sort(key=lambda d: (d["edge"] is None, -(d["edge"] or 0)))
    return out


def score_buckets(days: int = None, timeframe: str = None) -> List[Dict]:
    """Does a higher composite score actually predict a better outcome?"""
    closed = [r for r in _load(days, timeframe)
              if r.status in CLOSED_STATES and r.composite_score is not None]
    edges = [(0.70, 0.78), (0.78, 0.86), (0.86, 0.94), (0.94, 1.01)]
    out = []
    for lo, hi in edges:
        rows = [r for r in closed if lo <= r.composite_score < hi]
        wins = [r for r in rows if (r.pnl or 0) > 0]
        r_vals = [r.r_multiple for r in rows if r.r_multiple is not None]
        out.append({
            "band": f"{lo:.2f}-{hi if hi <= 1 else 1.0:.2f}",
            "trades": len(rows),
            "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else None,
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
            "pnl": round(sum(r.pnl or 0 for r in rows), 2),
        })
    return out


def breakdown(field: str, days: int = None, timeframe: str = None) -> List[Dict]:
    """Group closed trades by symbol or direction."""
    if field not in ("symbol", "direction"):
        raise ValueError("breakdown() supports 'symbol' or 'direction'")
    closed = [r for r in _load(days, timeframe) if r.status in CLOSED_STATES]
    groups: Dict[str, List[Signal]] = {}
    for r in closed:
        groups.setdefault(getattr(r, field) or "-", []).append(r)

    out = []
    for key, rows in groups.items():
        wins = [r for r in rows if (r.pnl or 0) > 0]
        r_vals = [r.r_multiple for r in rows if r.r_multiple is not None]
        out.append({
            "key": key,
            "trades": len(rows),
            "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
            "pnl": round(sum(r.pnl or 0 for r in rows), 2),
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
        })
    out.sort(key=lambda d: -d["pnl"])
    return out


def equity_curve(days: int = None, timeframe: str = None) -> List[Dict]:
    closed = [r for r in _load(days, timeframe) if r.status in CLOSED_STATES]
    curve, running, peak = [], 0.0, 0.0
    for r in closed:
        running += r.pnl or 0
        peak = max(peak, running)
        curve.append({
            "t": r.timestamp.strftime("%d-%m %H:%M") if r.timestamp else "",
            "symbol": r.symbol,
            "pnl": round(running, 2),
            "drawdown": round(running - peak, 2),
        })
    return curve


def option_gate_summary(days: int = None, timeframe: str = None) -> Dict:
    """How often the options overlay blocked, and why."""
    rows = _load(days, timeframe)
    blocked = [r for r in rows if r.option_blocked]
    reasons: Dict[str, int] = {}
    for r in blocked:
        key = (r.option_block_reason or "unknown").split(" - ")[0][:60]
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "total": len(rows),
        "blocked": len(blocked),
        "blocked_pct": round(len(blocked) / len(rows) * 100, 1) if rows else 0.0,
        "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
    }


def to_csv(days: int = None, timeframe: str = None) -> str:
    """Full ledger export, for when you'd rather analyse this in a spreadsheet."""
    import csv
    import io

    cols = ["id", "timestamp", "symbol", "timeframe", "direction", "entry", "sl",
            "trail_sl", "tp1", "tp2", "tp3", "qty", "lots", "atr14",
            "composite_score", "status", "tp1_hit", "tp2_hit", "exit_price",
            "exit_time", "pnl", "r_multiple", "atm_strike", "option_type",
            "option_expiry", "option_dte", "option_ltp", "option_iv",
            "option_iv_rank", "option_delta", "option_blocked",
            "option_block_reason", "notes"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols + ["outcome"])
    for r in _load(days, timeframe):
        row = []
        for c in cols:
            v = getattr(r, c, None)
            row.append(v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v)
        row.append(_classify(r) if r.status in CLOSED_STATES else "open")
        writer.writerow(row)
    return buf.getvalue()


def full_report(days: int = None, timeframe: str = None) -> Dict:
    tf = timeframe or cfg.TIMEFRAME
    return {
        "window_days": days,
        "timeframe": tf,
        "headline": headline(days, tf),
        "outcomes": outcome_mix(days, tf),
        "factors": factor_postmortem(days, tf),
        "score_buckets": score_buckets(days, tf),
        "by_symbol": breakdown("symbol", days, tf),
        "by_direction": breakdown("direction", days, tf),
        "options": option_gate_summary(days, tf),
    }
