"""
Sector trend layer (rule 12, 1 Nov 2026) - soft, never blocks a signal.

For a stock breakout, look up the stock's NSE sector index and call it:
  aligned  (+1)  index above its 15-min EMA20 AND above today's open (CALL),
                 or below both (PUT);
  against  (-1)  both point the other way;
  neutral  ( 0)  they disagree, or there is no sector index / no data.

NSE sector indices publish no volume, so intraday VWAP is not available; the
user chose "EMA20 and day-open must agree" (1 Nov 2026).

Aligned setups are ranked first for the daily cap; against ones last. The
result is stored on the signal (factor_breakdown: sector_index, sector_trend,
sector_note) and shown on the dashboard and in the alert.
"""
import logging
import time
from typing import Dict, Optional, Tuple

from config import cfg
from indicators import ema

logger = logging.getLogger(__name__)

# Banks get NIFTY BANK; the rest of financial services NIFTY FIN SERVICE.
BANKS = {
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN", "INDUSINDBK", "BANKBARODA", "PNB",
    "AUBANK", "IDFCFIRSTB", "FEDERALBNK", "CANBK", "BANKINDIA", "UNIONBANK", "INDIANB", "YESBANK",
    "RBLBANK", "BANDHANBNK", "KARURVYSYA", "CUB", "IOB", "UCOBANK", "CENTRALBK", "MAHABANK",
}

# NSE "Industry" (Nifty 500 file) -> candidate sector index tradingsymbols on
# Kite, first one found in the instruments dump wins (resolved at runtime and
# logged once). Textiles / Diversified have no index -> neutral.
INDUSTRY_INDEX = {
    "financial services": ["NIFTY FIN SERVICE"],
    "information technology": ["NIFTY IT"],
    "automobile and auto components": ["NIFTY AUTO"],
    "healthcare": ["NIFTY PHARMA", "NIFTY HEALTHCARE"],
    "fast moving consumer goods": ["NIFTY FMCG"],
    "metals & mining": ["NIFTY METAL"],
    "oil gas & consumable fuels": ["NIFTY OIL AND GAS", "NIFTY OIL & GAS", "NIFTY ENERGY"],
    "power": ["NIFTY ENERGY"],
    "realty": ["NIFTY REALTY"],
    "media entertainment & publication": ["NIFTY MEDIA"],
    "consumer durables": ["NIFTY CONSR DURBL", "NIFTY CONSUMER DURABLES"],
    "capital goods": ["NIFTY IND MANUFACTURING", "NIFTY INDIA MFG", "NIFTY CPSE", "NIFTY INFRA"],
    "construction": ["NIFTY INFRA"],
    "construction materials": ["NIFTY COMMODITIES"],
    "chemicals": ["NIFTY CHEMICALS", "NIFTY COMMODITIES"],
    "telecommunication": ["NIFTY INFRA"],
    "consumer services": ["NIFTY CONSUMPTION", "NIFTY INDIA CONSUMPTION"],
    "services": ["NIFTY SERV SECTOR", "NIFTY SERVICES SECTOR"],
}
_resolved: Dict[str, Optional[str]] = {}

_cache: Dict[str, Tuple[float, Dict]] = {}
TTL_SEC = 240


def sector_index_for(symbol: str, feed=None) -> Optional[str]:
    sym = symbol.upper()
    if sym in BANKS:
        return "NIFTY BANK"
    try:
        import sectors
        ind = sectors.load().get(sym)
    except Exception:
        ind = None
    if not ind:
        return None
    key = ind.strip().lower()
    cands = INDUSTRY_INDEX.get(key) or []
    if feed is None or not hasattr(feed, "_instrument"):
        return cands[0] if cands else None
    if key not in _resolved:
        found = None
        for c in cands:
            try:
                feed._instrument(c, False)
                found = c
                break
            except Exception:
                continue
        _resolved[key] = found
        logger.info("[Sector] %s -> %s", ind, found or "no index on Kite (neutral)")
    return _resolved[key]


async def index_state(feed, index: str) -> Optional[Dict]:
    """{'close', 'ema20', 'day_open', 'bias'} for the index, cached a few minutes."""
    hit = _cache.get(index)
    if hit and time.monotonic() - hit[0] < TTL_SEC:
        return hit[1]
    try:
        df = await feed.get_historical(index, "15m", 80, use_futures=False)
    except Exception as exc:
        logger.debug("[Sector] %s candles unavailable: %s", index, exc)
        _cache[index] = (time.monotonic(), None)
        return None
    if df is None or len(df) < 25:
        return None
    import pandas as pd
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    last_day = df["timestamp"].dt.date.iloc[-1]
    day = df[df["timestamp"].dt.date == last_day]
    close = float(df["close"].iloc[-1])
    e20 = float(ema(df["close"], 20).iloc[-1])
    day_open = float(day["open"].iloc[0])
    up = close > e20 and close > day_open
    dn = close < e20 and close < day_open
    st = {"close": round(close, 2), "ema20": round(e20, 2), "day_open": round(day_open, 2),
          "bias": 1 if up else (-1 if dn else 0)}
    _cache[index] = (time.monotonic(), st)
    return st


async def tag(feed, setup: Dict) -> None:
    """Add sector_index / sector_trend / sector_note to setup['meta']."""
    meta = setup.setdefault("meta", {})
    if not cfg.SECTOR_TREND or cfg.is_index(setup["symbol"]):
        return
    index = sector_index_for(setup["symbol"], feed)
    if not index:
        meta.update(sector_index=None, sector_trend=0, sector_note="no sector index")
        return
    st = await index_state(feed, index)
    if not st:
        meta.update(sector_index=index, sector_trend=0, sector_note=f"{index}: no data")
        return
    want = 1 if setup["direction"] == "LONG" else -1
    trend = 0 if st["bias"] == 0 else (1 if st["bias"] == want else -1)
    word = {1: "aligned", 0: "neutral", -1: "against"}[trend]
    meta.update(sector_index=index, sector_trend=trend,
                sector_note=(f"{index} {word}: {st['close']:.0f} vs EMA20 {st['ema20']:.0f}, "
                             f"open {st['day_open']:.0f}"))


def rank_key(setup: Dict) -> float:
    """Composite, plus 0.10 when the sector is aligned, minus 0.10 against or LOW-ADX."""
    m = setup.get("meta") or {}
    return ((setup.get("composite_score") or 0.0) + 0.10 * (m.get("sector_trend") or 0)
            - (0.10 if m.get("adx_low") else 0.0)
            - (0.10 if m.get("rsi_stretched") else 0.0))
