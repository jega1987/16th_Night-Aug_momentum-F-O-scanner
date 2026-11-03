"""
Redis live state (3 Nov 2026). Optional: with no REDIS_URL, or Redis down,
every function is a quiet no-op and the scanner runs exactly as before.

What lives here (all small, all fast):
  worker:heartbeat        worker alive, last scan time/duration (TTL 90 s)
  scan:last               summary of the last 5-min scan
  pos:<id> / positions    live state of each open trade (prices, stop, P&L)
  events (stream)         signals, exits, early alerts - last 2,000
  live (pub/sub)          the same events pushed to any listener
  trading:kill            kill switch: when set, no new signals/orders
  early:<date>            early alerts already sent today (survives restarts)
  lock:<name>             one-shot locks (no duplicate signal / order)

Postgres stays the record of truth; Redis is the fast, shared "now".
"""
import json
import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from clock import now_naive
from config import cfg

logger = logging.getLogger(__name__)

_client = None
_down_until = 0.0
_warned = False


def client():
    """Redis client, or None. A failed connect is retried after 60 s."""
    global _client, _down_until, _warned
    if not cfg.REDIS_URL:
        return None
    if _client is not None:
        return _client
    if time.monotonic() < _down_until:
        return None
    try:
        import redis
        c = redis.Redis.from_url(cfg.REDIS_URL, socket_timeout=0.5, socket_connect_timeout=1.0,
                                 decode_responses=True, health_check_interval=30)
        c.ping()
        _client = c
        logger.info("[Redis] Connected")
        _warned = False
        return _client
    except Exception as exc:
        _down_until = time.monotonic() + 60
        if not _warned:
            logger.warning("[Redis] Unavailable (%s) - running without live state", exc)
            _warned = True
        return None


def _safe(fn, default=None):
    global _client, _down_until
    c = client()
    if c is None:
        return default
    try:
        return fn(c)
    except Exception as exc:
        logger.debug("[Redis] call failed: %s", exc)
        _client = None
        _down_until = time.monotonic() + 30
        return default


def _jsonable(v: Any):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _dump(d: Dict) -> str:
    return json.dumps({k: _jsonable(v) for k, v in d.items()}, default=str)


# ---------------------------------------------------------------- events -- #
def publish(kind: str, payload: Dict) -> None:
    """Record an event in the 'events' stream and push it on the 'live' channel."""
    msg = {"kind": kind, "at": now_naive().isoformat(timespec="seconds"), **payload}
    raw = _dump(msg)

    def go(c):
        p = c.pipeline()
        p.xadd("events", {"data": raw}, maxlen=2000, approximate=True)
        p.publish("live", raw)
        p.execute()
    _safe(go)


def recent_events(n: int = 50) -> List[Dict]:
    def go(c):
        rows = c.xrevrange("events", count=n)
        return [json.loads(f["data"]) for _id, f in rows]
    return _safe(go, []) or []


# ------------------------------------------------------------- heartbeat -- #
def heartbeat(extra: Optional[Dict] = None) -> None:
    data = {"at": now_naive().isoformat(timespec="seconds"), **(extra or {})}
    _safe(lambda c: c.set("worker:heartbeat", _dump(data), ex=90))


def scan_summary(summary: Dict) -> None:
    _safe(lambda c: c.set("scan:last", _dump(summary), ex=3600))


def status() -> Dict:
    def go(c):
        hb = c.get("worker:heartbeat")
        sc = c.get("scan:last")
        return {"redis": True, "worker": json.loads(hb) if hb else None,
                "last_scan": json.loads(sc) if sc else None,
                "kill_switch": bool(c.get("trading:kill")),
                "open_positions": c.scard("positions")}
    return _safe(go, {"redis": False}) or {"redis": False}


# ------------------------------------------------------------- positions -- #
POS_FIELDS = ("id", "symbol", "direction", "status", "entry", "sl", "trail_sl", "qty_open",
              "pnl", "opt_entry", "opt_sl", "opt_trail_sl", "opt_tp1", "opt_tp2", "opt_tp3",
              "opt_last_now", "opt_bid_now", "opt_ask_now", "opt_ltp_now", "opt_pnl",
              "opt_updated_at", "atm_strike", "option_type", "manage_basis", "tp1_hit", "tp2_hit")


def put_position(s: Dict) -> None:
    sid = s.get("id")
    if sid is None:
        return
    data = {k: s.get(k) for k in POS_FIELDS}

    def go(c):
        p = c.pipeline()
        p.set(f"pos:{sid}", _dump(data), ex=6 * 3600)
        p.sadd("positions", sid)
        p.execute()
    _safe(go)


def drop_position(sid) -> None:
    def go(c):
        p = c.pipeline()
        p.delete(f"pos:{sid}")
        p.srem("positions", sid)
        p.execute()
    _safe(go)


def positions() -> List[Dict]:
    def go(c):
        ids = list(c.smembers("positions"))
        if not ids:
            return []
        vals = c.mget([f"pos:{i}" for i in ids])
        stale = [i for i, v in zip(ids, vals) if v is None]
        if stale:
            c.srem("positions", *stale)
        return [json.loads(v) for v in vals if v]
    return _safe(go, []) or []


# ----------------------------------------------------- kill switch, locks -- #
def kill_switch_on() -> bool:
    return bool(_safe(lambda c: c.get("trading:kill"), None))


def set_kill_switch(on: bool, who: str = "dashboard") -> bool:
    def go(c):
        if on:
            c.set("trading:kill", _dump({"by": who, "at": now_naive().isoformat()}))
        else:
            c.delete("trading:kill")
        return True
    ok = bool(_safe(go, False))
    if ok:
        publish("kill_switch", {"on": on, "by": who})
    return ok


def acquire(name: str, ttl_sec: int = 600) -> bool:
    """
    One-shot lock. True if this caller got it. Without Redis it returns True
    (single worker) so nothing is blocked.
    """
    got = _safe(lambda c: c.set(f"lock:{name}", "1", nx=True, ex=ttl_sec), "nored")
    if got == "nored":
        return True
    return bool(got)


def early_seen(key: str) -> bool:
    """True if this early alert was already sent today (also marks it sent)."""
    day = now_naive().date().isoformat()

    def go(c):
        added = c.sadd(f"early:{day}", key)
        c.expire(f"early:{day}", 2 * 86400)
        return added == 0
    return bool(_safe(go, False))
