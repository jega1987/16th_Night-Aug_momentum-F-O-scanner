"""
Feed interface. The scanner only ever talks to this contract, so swapping
5paisa for Zerodha/Dhan/Fyers later means writing one new class, not editing
the strategy.
"""
import asyncio
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import pandas as pd


class RateLimiter:
    """
    Token bucket. A semaphore limits how many calls run *at once*; it does not
    stop 200 symbols firing 200 calls in two seconds as each one completes.
    Broker limits are calls-per-interval, so that is what this enforces.
    """

    def __init__(self, calls: int, per_seconds: float = 1.0):
        self.capacity = max(1, calls)
        self.per_seconds = per_seconds
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.total_waited = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity,
                                   self._tokens + elapsed * (self.capacity / self.per_seconds))
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * (self.per_seconds / self.capacity)
                self.total_waited += wait
                await asyncio.sleep(wait)

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class MarketFeed(ABC):
    name: str = "base"

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def get_historical(self, symbol: str, interval: str = "5m", bars: int = 200,
                             use_futures: bool = True, from_date=None) -> pd.DataFrame:
        """
        Return a DataFrame with REQUIRED_COLUMNS, oldest bar first.
        `from_date` lets the candle store ask for only the missing window.
        """

    @abstractmethod
    async def get_live_quote(self, symbol: str, use_futures: bool = False) -> Dict:
        """ltp, open, high, low, prev_close, volume, oi."""

    async def get_oi(self, symbol: str) -> Optional[float]:
        """Current open interest for the tracked contract, or None."""
        return None

    async def get_fno_stocks(self) -> List[Dict]:
        """
        Stock futures from the scrip master, nearest expiry only.
        Each entry: {symbol, root, scrip_code, exch, exch_type, lot_size, expiry}
        """
        return []

    async def get_bulk_quotes(self, instruments: List[Dict]) -> Dict[str, Dict]:
        """Quotes for many scrips in as few calls as the API allows."""
        return {}

    async def get_expiries(self, symbol: str) -> List[Dict]:
        """
        Tradable option expiries, nearest first.
        Each entry: {"date": datetime.date, "epoch_ms": int}
        """
        return []

    async def get_option_quote(self, symbol: str, expiry: Dict, strike: int,
                               opt_type: str) -> Optional[Dict]:
        """
        One contract's quote: {"ltp", "oi", "volume", "scrip_code"}.
        Returns None when the strike isn't listed.
        """
        return None

    async def get_universe(self) -> List[str]:
        from config import cfg
        return list(cfg.INDICES)

    @staticmethod
    def normalize(df: pd.DataFrame, bars: int) -> pd.DataFrame:
        """Lower-case columns, coerce numerics, sort ascending, trim to `bars`."""
        if df is None or len(df) == 0:
            raise ValueError("Empty candle payload")

        df = df.copy()
        df.columns = [str(c).lower().strip() for c in df.columns]
        renames = {
            "datetime": "timestamp", "date": "timestamp", "time": "timestamp",
            "vol": "volume", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
        }
        df = df.rename(columns={k: v for k, v in renames.items() if k in df.columns})

        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if "volume" in missing:
            df["volume"] = 0.0
            missing.remove("volume")
        if missing:
            raise ValueError(f"Candle payload missing columns: {missing}. Got {list(df.columns)}")

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = (df.dropna(subset=["timestamp", "open", "high", "low", "close"])
                .sort_values("timestamp")
                .drop_duplicates(subset="timestamp", keep="last")
                .reset_index(drop=True))
        df["volume"] = df["volume"].fillna(0.0)

        if len(df) > bars:
            df = df.iloc[-bars:].reset_index(drop=True)
        return df[REQUIRED_COLUMNS]
