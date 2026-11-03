"""
F&O ban list (3 Nov 2026). Kite Connect has no ban-list API, so the list comes
from NSE's daily file "Securities in Ban Period":

    https://nsearchives.nseindia.com/content/fo/fo_secban.csv

It names the trade date and the stocks whose F&O open interest crossed 95% of
the market-wide limit; in a ban, no new F&O positions may be opened (only
reduced). The scanner therefore records no new signal on a banned stock;
open positions are still managed and exited normally.

Fetched at 08:15 and re-checked through the day (the file can change after a
stock leaves the ban). Stored in fo_ban (trade_date, symbol) so a failed
download keeps the last list; a list for another date is not applied.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional, Set, Tuple

import httpx

from clock import now_naive
from config import cfg
from database import FoBan, session_scope

logger = logging.getLogger(__name__)
_cache: Tuple[Optional[date], Set[str]] = (None, set())
_cache_at: Optional[datetime] = None


def parse(text: str) -> Tuple[Optional[date], Set[str]]:
    """'Securities in Ban For Trade Date 03-NOV-2026:' then '1,ABC' rows (or NIL)."""
    trade_date = None
    m = re.search(r"(\d{2}-[A-Za-z]{3}-\d{4})", text or "")
    if m:
        try:
            trade_date = datetime.strptime(m.group(1).title(), "%d-%b-%Y").date()
        except ValueError:
            trade_date = None
    symbols = set()
    for line in (text or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and re.fullmatch(r"[A-Z0-9&\-]+", parts[1].upper()):
            symbols.add(parts[1].upper())
    return trade_date, symbols


def refresh() -> int:
    """Download and store today's list. Returns the number of banned symbols, -1 on failure."""
    try:
        r = httpx.get(cfg.FO_BAN_URL, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (scanner ban list)",
                               "Accept": "text/csv,*/*"})
        r.raise_for_status()
        trade_date, symbols = parse(r.text)
        if trade_date is None:
            raise ValueError(f"no trade date in file: {r.text[:80]!r}")
    except Exception as exc:
        logger.warning("[Ban] Download failed, keeping the stored list: %s", exc)
        return -1
    with session_scope() as db:
        db.query(FoBan).filter(FoBan.trade_date == trade_date).delete()
        db.add_all([FoBan(trade_date=trade_date, symbol=s, updated_at=now_naive()) for s in symbols])
    global _cache_at
    _cache_at = None
    logger.info("[Ban] %s: %d stock(s) in F&O ban%s", trade_date, len(symbols),
                (" - " + ", ".join(sorted(symbols))) if symbols else "")
    try:
        import livestate
        livestate.publish("ban_list", {"date": trade_date.isoformat(), "symbols": sorted(symbols)})
    except Exception:
        pass
    return len(symbols)


def banned_today() -> Set[str]:
    """Symbols banned for today's trade date (empty if today's list is unknown)."""
    global _cache, _cache_at
    today = now_naive().date()
    if _cache_at and (now_naive() - _cache_at).total_seconds() < 300 and _cache[0] == today:
        return _cache[1]
    try:
        with session_scope() as db:
            syms = {r.symbol for r in db.query(FoBan).filter(FoBan.trade_date == today).all()}
        _cache, _cache_at = (today, syms), now_naive()
    except Exception as exc:
        logger.debug("[Ban] list unavailable: %s", exc)
    return _cache[1] if _cache[0] == today else set()


def has_today() -> bool:
    try:
        with session_scope() as db:
            return db.query(FoBan).filter(FoBan.trade_date == now_naive().date()).count() > 0
    except Exception:
        return False


def is_banned(symbol: str) -> bool:
    return cfg.FO_BAN_FILTER and symbol.upper() in banned_today()
