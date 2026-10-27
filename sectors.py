"""
Sector of each stock, for the per-sector position cap.

Source: NSE's Nifty 500 constituents file (Company, Industry, Symbol, ...),
which covers practically all F&O stocks. Downloaded daily before the open and
kept in the sector_map table, so a failed download keeps yesterday's map.
Symbols not in the map are treated as their own sector (never capped
together with anything else).
"""
import csv
import io
import logging
from typing import Dict

import httpx

from clock import now_naive
from config import cfg
from database import SectorMap, session_scope

logger = logging.getLogger(__name__)
_cache: Dict[str, str] = {}
_cache_at = None


def refresh() -> int:
    """Download and store the map. Returns rows stored (0 on failure)."""
    try:
        r = httpx.get(cfg.SECTOR_SOURCE_URL, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (scanner sector map)",
                               "Accept": "text/csv,*/*"})
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        mapping = {}
        for row in rows:
            sym = (row.get("Symbol") or "").strip().upper()
            ind = (row.get("Industry") or "").strip()
            if sym and ind:
                mapping[sym] = ind
        if len(mapping) < 100:
            raise ValueError(f"only {len(mapping)} rows parsed")
    except Exception as exc:
        logger.warning("[Sectors] Download failed, keeping the stored map: %s", exc)
        return 0
    now = now_naive()
    with session_scope() as db:
        db.query(SectorMap).delete()
        db.add_all([SectorMap(symbol=k, industry=v, updated_at=now) for k, v in mapping.items()])
    global _cache_at
    _cache_at = None
    logger.info("[Sectors] Stored %d symbols", len(mapping))
    return len(mapping)


def load() -> Dict[str, str]:
    global _cache, _cache_at
    if _cache_at and (now_naive() - _cache_at).total_seconds() < 3600:
        return _cache
    try:
        with session_scope() as db:
            _cache = {r.symbol: r.industry for r in db.query(SectorMap).all()}
        _cache_at = now_naive()
    except Exception as exc:
        logger.debug("[Sectors] Map unavailable: %s", exc)
    return _cache


def sector_of(symbol: str) -> str:
    return load().get(symbol.upper(), f"_{symbol.upper()}")
