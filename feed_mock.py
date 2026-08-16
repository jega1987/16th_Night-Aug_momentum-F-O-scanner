"""
Synthetic feed for local development, CI, and a first Railway deploy before
credentials are wired up. Set FEED_MODE=mock.

It generates volatility regimes - quiet compression followed by expansion - so
the squeeze logic actually has something to detect. Nothing here touches a
broker, and the numbers are fake: never judge the strategy on mock output.
"""
import asyncio
import logging
import random
from datetime import datetime, time as dtime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from clock import now_naive
from config import cfg
from feed_base import MarketFeed

logger = logging.getLogger(__name__)

BASE_PRICES = {
    "RELIANCE": 2900.0, "TCS": 4100.0, "HDFCBANK": 1700.0, "INFY": 1850.0,"NIFTY 50": 24500.0, "BANKNIFTY": 52000.0, "SENSEX": 80500.0, "FINNIFTY": 23400.0}
INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "60m": 60, "1d": 375}


class MockFeed(MarketFeed):
    name = "mock"

    def __init__(self, seed: Optional[int] = None):
        self.connected = False
        self._rng = random.Random(seed)
        self._np = np.random.default_rng(seed)
        self._oi: Dict[str, float] = {}
        self._base_iv: Dict[str, float] = {}

    async def connect(self) -> bool:
        await asyncio.sleep(0)
        self.connected = True
        logger.warning("[MockFeed] Running on SYNTHETIC data - no broker connection")
        return True

    def _base(self, symbol: str) -> float:
        return BASE_PRICES.get(symbol, 20000.0)

    async def get_historical(self, symbol: str, interval: str = "5m", bars: int = 200,
                             use_futures: bool = True, from_date=None) -> pd.DataFrame:
        await asyncio.sleep(0.02)
        step = INTERVAL_MINUTES.get(interval, 5)
        price = self._base(symbol)
        vol_unit = price * 0.0006

        # Alternate compression and expansion regimes.
        regimes, i = [], 0
        while i < bars:
            quiet = self._rng.random() < 0.5
            length = self._rng.randint(8, 18)
            regimes += [0.35 if quiet else 1.6] * length
            i += length
        regimes = regimes[:bars]

        rows = []
        end = now_naive().replace(second=0, microsecond=0)
        for i in range(bars):
            k = regimes[i]
            drift = self._np.normal(0, vol_unit * k)
            o = price
            c = max(1.0, o + drift)
            wick = abs(self._np.normal(0, vol_unit * k * 0.6))
            h = max(o, c) + wick
            l = min(o, c) - wick
            base_vol = 90_000 if k < 1 else 260_000
            v = int(abs(self._np.normal(base_vol, base_vol * 0.25)))
            rows.append({
                "timestamp": end - timedelta(minutes=step * (bars - i)),
                "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
                "close": round(c, 2), "volume": v,
            })
            price = c

        out = self.normalize(pd.DataFrame(rows), bars)
        # Kite embeds OI in candles; mirror that so the same code path is tested.
        base_oi = 10_000_000
        drift = self._np.normal(0.001, 0.006, size=len(out)).cumsum()
        out["oi"] = (base_oi * (1 + drift)).round(0)
        return out

    async def get_live_quote(self, symbol: str, use_futures: bool = False) -> Dict:
        await asyncio.sleep(0.01)
        base = self._base(symbol)
        ltp = round(base * (1 + self._np.normal(0, 0.004)), 2)
        prev_close = round(base * (1 + self._np.normal(0, 0.003)), 2)
        return {
            "ltp": ltp,
            "open": prev_close,
            "high": round(max(ltp, prev_close) * 1.003, 2),
            "low": round(min(ltp, prev_close) * 0.997, 2),
            "prev_close": prev_close,
            "volume": int(abs(self._np.normal(5_000_000, 800_000))),
            "oi": int(self._oi.get(symbol, 12_000_000)),
        }

    async def get_oi(self, symbol: str) -> Optional[float]:
        cur = self._oi.get(symbol, 12_000_000.0)
        cur *= 1 + self._np.normal(0.002, 0.01)
        self._oi[symbol] = cur
        return round(cur, 0)

    # ------------------------------------------------------------------ #
    async def get_fno_stocks(self) -> List[Dict]:
        """A synthetic F&O list spanning the price floor in both directions."""
        await asyncio.sleep(0.01)
        names = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "AXISBANK",
                 "ITC", "LT", "BHARTIARTL", "MARUTI", "TATASTEEL", "TATAMOTORS", "WIPRO",
                 "HINDALCO", "ADANIENT", "BAJFINANCE", "SUNPHARMA", "TITAN", "ONGC",
                 "IDEA", "YESBANK", "PNB", "IOB", "SUZLON", "NHPC"]   # last six are cheap
        today = now_naive().date()
        expiry = today + timedelta(days=(1 - today.weekday()) % 7 + 21)
        out = []
        for i, n in enumerate(names):
            out.append({"symbol": n, "root": n, "scrip_code": 100000 + i,
                        "exch": "N", "exch_type": "D",
                        "lot_size": int(self._rng.choice([250, 500, 1000, 2500])),
                        "expiry": expiry.isoformat()})
        return out

    async def get_bulk_quotes(self, instruments: List[Dict]) -> Dict[str, Dict]:
        await asyncio.sleep(0.01)
        cheap = {"IDEA", "YESBANK", "PNB", "IOB", "SUZLON", "NHPC"}
        out = {}
        for inst in instruments:
            sym = inst["symbol"]
            price = round(self._rng.uniform(8, 190), 2) if sym in cheap \
                else round(self._rng.uniform(210, 4200), 2)
            # Turnover spread across three orders of magnitude, as in reality.
            volume = int(abs(self._np.normal(1, 0.6) * self._rng.choice([2e5, 2e6, 1.2e7])))
            out[sym] = {
                "ltp": price, "volume": volume,
                "prev_close": round(price * (1 + self._np.normal(0, 0.018)), 2),
                "high": round(price * 1.012, 2), "low": round(price * 0.988, 2),
            }
        return out

    async def get_expiries(self, symbol: str) -> List[Dict]:
        """Weekly Thursdays for the next month, plus the monthly."""
        await asyncio.sleep(0)
        today = now_naive().date()
        out = []
        for week in range(5):
            day = today + timedelta(days=(3 - today.weekday()) % 7 + 7 * week)
            out.append({"date": day,
                        "epoch_ms": int(datetime.combine(day, dtime(15, 30)).timestamp() * 1000)})
        return out

    async def get_option_quote(self, symbol: str, expiry: Dict, strike: int,
                               opt_type: str) -> Optional[Dict]:
        """
        Price the strike with Black-76 off a slowly drifting base vol, so IV
        inversion in the tests recovers something close to the input and the
        IV-rank history has genuine variation to rank.
        """
        await asyncio.sleep(0.01)
        from options_layer import black76_price, years_to_expiry

        quote = await self.get_live_quote(symbol, use_futures=True)
        futures = float(quote["ltp"])
        t = years_to_expiry(expiry["date"])
        if t <= 0:
            return None

        base = self._base_iv.get(symbol, 0.14)
        base = float(min(0.45, max(0.07, base * (1 + self._np.normal(0, 0.05)))))
        self._base_iv[symbol] = base

        # A crude smile: strikes away from the money trade at higher vol.
        moneyness = abs(strike - futures) / max(futures, 1.0)
        iv = base * (1 + 4.0 * moneyness)

        price = black76_price(futures, strike, t, iv, opt_type.upper() == "CE")
        if price <= 0.05:
            return None
        # Spread widens away from the money, as it does in a real book.
        spread = max(0.05, round(price * (0.004 + 3.0 * moneyness), 2))
        bid = round(max(0.05, price - spread / 2), 2)
        ask = round(bid + spread, 2)
        return {
            "ltp": round((bid + ask) / 2, 2),
            "last_price": round(price, 2),
            "bid": bid, "ask": ask, "spread": round(ask - bid, 2),
            "spread_pct": round((ask - bid) / price * 100, 2),
            "oi": int(abs(self._np.normal(900_000, 200_000))),
            "volume": int(abs(self._np.normal(120_000, 40_000))),
            "scrip_code": abs(hash((symbol, strike, opt_type))) % 900000 + 100000,
        }


def build_feed() -> MarketFeed:
    """
    Factory used by the app. Kite is the only live broker; FEED_MODE=mock skips
    the broker entirely.

    An unrecognised mode raises rather than falling back to a default. A silent
    fallback here once caused the app to quietly talk to the wrong broker while
    its own startup banner reported the right one - hours were lost to that.
    """
    mode = cfg.FEED_MODE
    if mode == "mock":
        return MockFeed()
    if mode == "kite":
        from feed_kite import KiteFeed
        return KiteFeed()
    raise ValueError(
        f"Unknown FEED_MODE '{mode}'. Valid values are 'kite' or 'mock'.")
