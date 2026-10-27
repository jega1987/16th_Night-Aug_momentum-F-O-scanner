"""
Portfolio-level risk rules applied before new signals are recorded.

Loss limits, in R (multiples of each trade's initial risk):
  day  - trades closed today + trades opened today and still open (marked)
  week - the same from Monday
Hitting -DAILY_LOSS_LIMIT_R stops new signals for the rest of the day;
-WEEKLY_LOSS_LIMIT_R stops them until next Monday. Open positions are still
managed normally.

Sector cap: at most SECTOR_MAX_OPEN positions in the same NSE industry and
direction, counting open positions and setups accepted earlier in the scan.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from clock import now_naive
from config import cfg
from database import Signal, session_scope

logger = logging.getLogger(__name__)
OPEN = ("OPEN", "RUNNING")


def _r(sig: Signal) -> float:
    if sig.r_multiple is not None and sig.status not in OPEN:
        return float(sig.r_multiple)
    risk = abs((sig.entry or 0) - (sig.sl or 0)) * (sig.qty or 0)
    return float(sig.pnl or 0) / risk if risk > 0 else 0.0


def period_r(since) -> float:
    with session_scope() as db:
        closed = db.query(Signal).filter(Signal.exit_time >= since,
                                         ~Signal.status.in_(OPEN)).all()
        opened = db.query(Signal).filter(Signal.timestamp >= since,
                                         Signal.status.in_(OPEN)).all()
        return round(sum(_r(s) for s in closed) + sum(_r(s) for s in opened), 2)


def loss_limit_reason(now=None) -> Optional[str]:
    now = now or now_naive()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    if cfg.DAILY_LOSS_LIMIT_R > 0:
        r = period_r(day)
        if r <= -cfg.DAILY_LOSS_LIMIT_R:
            return f"Daily loss limit: {r:+.2f}R today (limit -{cfg.DAILY_LOSS_LIMIT_R:g}R)"
    if cfg.WEEKLY_LOSS_LIMIT_R > 0:
        r = period_r(week)
        if r <= -cfg.WEEKLY_LOSS_LIMIT_R:
            return f"Weekly loss limit: {r:+.2f}R this week (limit -{cfg.WEEKLY_LOSS_LIMIT_R:g}R)"
    return None


def apply_sector_cap(setups: List[Dict]) -> List[Dict]:
    if cfg.SECTOR_MAX_OPEN <= 0 or not setups:
        return setups
    import sectors
    with session_scope() as db:
        held = [(s.symbol, s.direction) for s in
                db.query(Signal).filter(Signal.status.in_(OPEN)).all()]
    count: Dict = {}
    for sym, d in held:
        if not cfg.is_index(sym):
            key = (sectors.sector_of(sym), d)
            count[key] = count.get(key, 0) + 1
    kept = []
    for st in sorted(setups, key=lambda x: -(x.get("composite_score") or 0)):
        if cfg.is_index(st["symbol"]):
            kept.append(st)
            continue
        key = (sectors.sector_of(st["symbol"]), st["direction"])
        if count.get(key, 0) >= cfg.SECTOR_MAX_OPEN:
            logger.info("[Risk] %s %s skipped - %d %s position(s) already in %s",
                        st["symbol"], st["direction"], count[key], st["direction"], key[0])
            continue
        count[key] = count.get(key, 0) + 1
        kept.append(st)
    return kept
