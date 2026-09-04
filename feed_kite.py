"""
Zerodha Kite Connect feed.

Verified against kiteconnect 5.2.x:

    KiteConnect(api_key=...)
    .login_url()                                  -> browser redirect
    .generate_session(request_token, api_secret)  -> {"access_token": ...}
    .set_access_token(token)
    .instruments(exchange)                        -> list of dicts
    .quote(["NSE:INFY", ...])                     -> dict keyed by exchange:tradingsymbol
    .historical_data(instrument_token, from_date, to_date, interval,
                     continuous=False, oi=False)  -> [{date, open, high, low, close, volume, oi}]

    KiteTicker(api_key, access_token)
    .connect(threaded=True)                       -> runs its own thread
    .subscribe(tokens) / .unsubscribe(tokens) / .set_mode(mode, tokens)
    callbacks: on_ticks(ws, ticks), on_connect, on_close, on_error

Three capabilities this module leans on:

* **Open interest arrives inside the candles** (`oi=True`). OI change is now a
  real per-bar series instead of a difference between two polled snapshots.
* **Ticks carry OI too**, so the streamed path doesn't lose it.
* **`quote()` returns depth**, so option bid/ask spread is available - which is
  what the liquidity gate needs.

The awkward part is the token. Kite access tokens expire every morning and the
`request_token` only arrives via a browser redirect, which does not fit an
always-on container. So the token is persisted in the database by the
/kite/callback route and reloaded on boot: one click each morning, no redeploy.
"""
import asyncio
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from clock import MarketClock, now_naive, today_start
from config import cfg
from database import BrokerToken, session_scope
from feed_base import MarketFeed, RateLimiter

logger = logging.getLogger(__name__)

try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError:                       # lets the app boot in mock mode
    KiteConnect = None
    KiteTicker = None


class CredentialsError(RuntimeError):
    """Configuration is wrong, missing, or the token has expired.
    Retrying on a timer will not fix any of these."""


# Kite interval names differ from the internal "5m" shorthand.
INTERVALS = {"1m": "minute", "3m": "3minute", "5m": "5minute", "10m": "10minute",
             "15m": "15minute", "30m": "30minute", "60m": "60minute", "1d": "day"}

# Maximum span Kite will serve in one historical request, per interval.
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100,
            "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000}

# Index spot instruments. Tokens are resolved from the instruments dump at
# startup rather than hardcoded, so these are only the lookup keys.
INDEX_LOOKUP = {
    "NIFTY 50":  {"exchange": "NSE", "tradingsymbol": "NIFTY 50",   "root": "NIFTY"},
    "BANKNIFTY": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "root": "BANKNIFTY"},
    "FINNIFTY":  {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "root": "FINNIFTY"},
    "SENSEX":    {"exchange": "BSE", "tradingsymbol": "SENSEX",     "root": "SENSEX"},
}
INDEX_ROOTS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}


class KiteFeed(MarketFeed):
    name = "kite"

    def __init__(self):
        self.kite = None
        self.connected = False
        self.last_error: Optional[str] = None
        self.stream = None
        self._sem = asyncio.Semaphore(cfg.MAX_CONCURRENT_FETCHES)
        # Kite's documented ceilings are roughly 3/s for historical and 1/s for
        # quote. One conservative limiter covers both.
        self._limiter = RateLimiter(max(1, int(cfg.API_CALLS_PER_SECOND)), 1.0)
        self._instruments: Dict[str, List[Dict]] = {}     # exchange -> rows
        self._token_map: Dict[str, Dict] = {}             # symbol -> instrument row
        self._quote_cache: Dict[str, Dict] = {}
        self._quote_cache_at: Dict[str, datetime] = {}
        self._chain_cache: Dict = {}
        self._chain_cache_at: Dict = {}

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        missing = cfg.missing_credentials()
        if missing:
            raise CredentialsError("Missing Railway variables: " + ", ".join(missing))
        if KiteConnect is None:
            raise CredentialsError(
                "kiteconnect is not installed in the image. The Docker build should have "
                "caught this - check the build log for 'all runtime dependencies present'.")

        token = cfg.KITE_ACCESS_TOKEN or self.load_token()
        if not token:
            raise CredentialsError(
                "No Kite access token. Open /kite/login in a browser and sign in - "
                "the token is stored automatically and lasts until tomorrow morning. "
                "Kite tokens expire daily and can only be issued through a browser "
                "redirect, so this is a once-a-day click rather than a redeploy.")

        def _login():
            client = KiteConnect(api_key=cfg.KITE_API_KEY)
            client.set_access_token(token)
            # Cheapest call that proves the token is live.
            client.profile()
            return client

        try:
            self.kite = await asyncio.to_thread(_login)
        except Exception as exc:
            if "token" in str(exc).lower() or "session" in str(exc).lower():
                self.clear_token()
                raise CredentialsError(
                    f"Kite rejected the stored token ({exc}). It has most likely expired - "
                    "Kite tokens die each morning. Open /kite/login to issue a new one.")
            raise

        self.connected = True
        self.last_error = None
        logger.info("[Kite] Authenticated")
        await self._load_instruments()
        return True

    # ---- token persistence -------------------------------------------- #
    @staticmethod
    def store_token(access_token: str, public_token: str = "", user_id: str = "") -> None:
        with session_scope() as db:
            db.query(BrokerToken).filter(BrokerToken.broker == "kite").delete(
                synchronize_session=False)
            db.add(BrokerToken(broker="kite", access_token=access_token,
                               public_token=public_token, user_id=user_id,
                               issued_at=now_naive()))
        logger.info("[Kite] Access token stored for %s", user_id or "user")

    @staticmethod
    def load_token() -> Optional[str]:
        """
        Today's token only. A token issued yesterday is already dead, and
        returning it would produce a confusing API error instead of a clear
        'please log in' message.
        """
        with session_scope() as db:
            row = (db.query(BrokerToken)
                     .filter(BrokerToken.broker == "kite")
                     .order_by(BrokerToken.issued_at.desc()).first())
            if not row or not row.access_token:
                return None
            if row.issued_at and row.issued_at < today_start():
                logger.info("[Kite] Stored token was issued %s - expired",
                            row.issued_at.date())
                return None
            return row.access_token

    @staticmethod
    def clear_token() -> None:
        with session_scope() as db:
            db.query(BrokerToken).filter(BrokerToken.broker == "kite").delete(
                synchronize_session=False)

    @staticmethod
    def login_url() -> str:
        if KiteConnect is None:
            return ""
        return KiteConnect(api_key=cfg.KITE_API_KEY).login_url()

    @classmethod
    def exchange_request_token(cls, request_token: str) -> Dict:
        """Called by the /kite/callback route after the browser redirect."""
        if KiteConnect is None:
            raise CredentialsError("kiteconnect is not installed")
        client = KiteConnect(api_key=cfg.KITE_API_KEY)
        data = client.generate_session(request_token, api_secret=cfg.KITE_API_SECRET)
        cls.store_token(data.get("access_token", ""),
                        data.get("public_token", ""),
                        data.get("user_id", ""))
        return data

    # ------------------------------------------------------------------ #
    # Instruments
    # ------------------------------------------------------------------ #
    async def _load_instruments(self) -> None:
        """
        The full dump is several MB, so it is fetched once per day and cached
        in memory. Everything downstream - index tokens, futures, the option
        chain, lot sizes - is derived from it.
        """
        for exchange in ("NSE", "NFO", "BSE", "BFO"):
            try:
                rows = await self._call(lambda ex=exchange: self.kite.instruments(ex),
                                        f"instruments {exchange}")
                self._instruments[exchange] = rows or []
                logger.info("[Kite] %s: %d instruments", exchange, len(rows or []))
            except Exception as exc:
                self._instruments[exchange] = []
                logger.error("[Kite] Could not load %s instruments: %s", exchange, exc)

        self._token_map = {}
        for symbol, meta in INDEX_LOOKUP.items():
            row = self._find_index(meta)
            if row:
                self._token_map[symbol] = row
            else:
                logger.warning("[Kite] Index %s not found in the %s dump",
                               symbol, meta["exchange"])

        # Nearest future per index, which is what carries volume and OI.
        for symbol, meta in INDEX_LOOKUP.items():
            fut = self._nearest_future(meta["root"])
            if fut:
                self._token_map[symbol + "::FUT"] = fut
                logger.info("[Kite] %s future -> %s (expiry %s, lot %s)",
                            symbol, fut.get("tradingsymbol"), fut.get("expiry"),
                            fut.get("lot_size"))

    def _find_index(self, meta: Dict) -> Optional[Dict]:
        want = meta["tradingsymbol"].upper()
        for row in self._instruments.get(meta["exchange"], []):
            if str(row.get("tradingsymbol", "")).upper() == want:
                return row
        return None

    def _nearest_future(self, root: str) -> Optional[Dict]:
        today = now_naive().date()
        best, best_expiry = None, None
        for exchange in ("NFO", "BFO"):
            for row in self._instruments.get(exchange, []):
                if str(row.get("instrument_type", "")).upper() != "FUT":
                    continue
                if str(row.get("name", "")).upper() != root.upper():
                    continue
                expiry = _as_date(row.get("expiry"))
                if not expiry or expiry < today:
                    continue
                if best_expiry is None or expiry < best_expiry:
                    best, best_expiry = row, expiry
        return best

    def _instrument(self, symbol: str, use_futures: bool) -> Dict:
        if use_futures and (symbol + "::FUT") in self._token_map:
            return self._token_map[symbol + "::FUT"]
        if symbol in self._token_map:
            return self._token_map[symbol]
        # Equities from the dynamic universe.
        row = self._find_equity(symbol, use_futures)
        if row:
            self._token_map[symbol + ("::FUT" if use_futures else "")] = row
            return row
        raise KeyError(f"No Kite instrument for '{symbol}'")

    def _find_equity(self, symbol: str, use_futures: bool) -> Optional[Dict]:
        if use_futures:
            return self._nearest_future(symbol)
        for row in self._instruments.get("NSE", []):
            if str(row.get("tradingsymbol", "")).upper() == symbol.upper():
                return row
        return None

    # ------------------------------------------------------------------ #
    # Candles
    # ------------------------------------------------------------------ #
    async def get_historical(self, symbol: str, interval: str = "5m", bars: int = 200,
                             use_futures: bool = True, from_date=None) -> pd.DataFrame:
        self._require_connection()
        kite_interval = INTERVALS.get(interval)
        if not kite_interval:
            raise ValueError(f"Unsupported interval '{interval}'. Allowed: {sorted(INTERVALS)}")

        inst = self._instrument(symbol, use_futures)
        token = int(inst["instrument_token"])

        to_dt = datetime.now()
        if from_date is None:
            per_day = {"minute": 375, "3minute": 125, "5minute": 75, "10minute": 38,
                       "15minute": 25, "30minute": 13, "60minute": 7, "day": 1}[kite_interval]
            days = max(7, int(bars / per_day * 1.8) + 3)
        else:
            base = from_date if isinstance(from_date, datetime) else \
                datetime.combine(from_date, datetime.min.time())
            days = max(1, (to_dt - base).days + 1)

        # Kite refuses spans beyond a per-interval ceiling rather than truncating.
        days = min(days, MAX_DAYS.get(kite_interval, 100))
        from_dt = to_dt - timedelta(days=days)

        rows = await self._call(
            lambda: self.kite.historical_data(token, from_dt, to_dt, kite_interval, oi=True),
            f"candles {symbol} {interval}")

        if not rows:
            raise ValueError(f"No candles for {symbol} {interval} "
                             f"(token {token}, futures={use_futures})")

        df = pd.DataFrame(rows)
        # Kite returns timezone-AWARE IST datetimes. The rest of the app - and
        # the database column - is naive IST. Passing aware values through let
        # Postgres silently convert them to UTC on insert, which both shifted
        # every stored bar by 5h30m and broke the store's duplicate check
        # (aware 11:50+05:30 never equals stored naive 11:50), producing
        # UniqueViolation crashes on backfill. Strip the tz at the boundary so
        # exactly one convention exists everywhere inside the app.
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        # Keep OI alongside the OHLCV - this is the real win over polling.
        oi_series = df["oi"].copy() if "oi" in df.columns else None
        out = self.normalize(df.rename(columns={"date": "timestamp"}), bars)
        if oi_series is not None:
            out["oi"] = pd.to_numeric(oi_series, errors="coerce").tail(len(out)).values
        return out

    # ------------------------------------------------------------------ #
    # Quotes
    # ------------------------------------------------------------------ #
    async def get_live_quote(self, symbol: str, use_futures: bool = False) -> Dict:
        """
        Returns the quote dict with `stale` set. `stale=False` means this call
        itself just got a live print from Kite. `stale=True` means Kite failed
        and this is the last good quote, replayed - the caller decides whether
        that's still useful (a display fallback) or should be treated as "no
        quote" (anything that persists a timestamp implying freshness).

        The cache is only served for QUOTE_CACHE_MAX_AGE_SECONDS after it was
        captured. Past that, a *persistent* failure (not a one-off blip) stops
        being masked as a live price that has simply stopped moving, and starts
        raising like the failure it is.
        """
        self._require_connection()
        inst = self._instrument(symbol, use_futures)
        key = f"{inst['exchange']}:{inst['tradingsymbol']}"
        try:
            data = await self._call(lambda: self.kite.quote([key]), f"quote {symbol}")
            row = (data or {}).get(key)
            if not row:
                raise ValueError("empty quote payload")
            quote = _quote_from(row)
            if quote["ltp"] <= 0:
                raise ValueError(f"quote returned ltp={quote['ltp']}")
            quote["stale"] = False
            self._quote_cache[symbol] = quote
            self._quote_cache_at[symbol] = now_naive()
            return quote
        except Exception as exc:
            cached_at = self._quote_cache_at.get(symbol)
            age = (now_naive() - cached_at).total_seconds() if cached_at else None
            if symbol in self._quote_cache and age is not None and age <= cfg.QUOTE_CACHE_MAX_AGE_SECONDS:
                logger.warning("[Kite] Quote failed for %s (%s) - serving %.0fs-old cache",
                               symbol, exc, age)
                stale_quote = dict(self._quote_cache[symbol])
                stale_quote["stale"] = True
                return stale_quote
            if symbol in self._quote_cache:
                logger.error("[Kite] Quote failed for %s (%s) - cache is %.0fs old, "
                             "past the %ds ceiling, refusing to serve it as live",
                             symbol, exc, age or -1, cfg.QUOTE_CACHE_MAX_AGE_SECONDS)
            raise

    async def get_oi(self, symbol: str) -> Optional[float]:
        try:
            quote = await self.get_live_quote(symbol, use_futures=True)
            oi = float(quote.get("oi") or 0)
            return oi if oi > 0 else None
        except Exception as exc:
            logger.warning("[Kite] OI unavailable for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------ #
    # Universe
    # ------------------------------------------------------------------ #
    async def get_fno_stocks(self) -> List[Dict]:
        """Stock futures from the NFO dump, nearest expiry per underlying."""
        self._require_connection()
        today = now_naive().date()
        best: Dict[str, Dict] = {}

        for row in self._instruments.get("NFO", []):
            if str(row.get("instrument_type", "")).upper() != "FUT":
                continue
            name = str(row.get("name", "")).upper()
            if not name or name in INDEX_ROOTS:
                continue
            expiry = _as_date(row.get("expiry"))
            if not expiry or expiry < today:
                continue
            current = best.get(name)
            if current is None or expiry < current["_expiry"]:
                best[name] = {**row, "_expiry": expiry}

        out = []
        for name, row in best.items():
            out.append({
                "symbol": name,
                "root": name,
                "scrip_code": int(row["instrument_token"]),
                "exch": "NSE",                    # the underlying trades on NSE cash
                "exch_type": "NFO",
                "lot_size": int(row.get("lot_size") or 0),
                "expiry": row["_expiry"].isoformat(),
            })
        logger.info("[Kite] %d stock futures in the NFO dump", len(out))
        return out

    async def get_bulk_quotes(self, instruments: List[Dict]) -> Dict[str, Dict]:
        """
        quote() takes a list, so many symbols cost one call. Chunked because
        Kite caps a single request at 500 instruments.
        """
        self._require_connection()
        out: Dict[str, Dict] = {}
        size = max(1, min(cfg.QUOTE_BATCH_SIZE, 250))
        chunks = [instruments[i:i + size] for i in range(0, len(instruments), size)]

        for chunk in chunks:
            keys, back = [], {}
            for c in chunk:
                key = f"NSE:{c['symbol']}"       # cash price decides the Rs 200 floor
                keys.append(key)
                back[key] = c["symbol"]
            try:
                data = await self._call(lambda k=keys: self.kite.quote(k),
                                        f"bulk quotes x{len(keys)}")
            except Exception as exc:
                logger.warning("[Kite] Bulk quote chunk failed: %s", exc)
                continue
            for key, row in (data or {}).items():
                symbol = back.get(key)
                if symbol and row:
                    out[symbol] = _quote_from(row)

        logger.info("[Kite] Quoted %d/%d instruments in %d call(s)",
                    len(out), len(instruments), len(chunks))
        return out

    # ------------------------------------------------------------------ #
    # Options
    # ------------------------------------------------------------------ #
    async def get_expiries(self, symbol: str) -> List[Dict]:
        """Kite has no expiry endpoint - derive it from the instruments dump."""
        self._require_connection()
        root = INDEX_LOOKUP.get(symbol, {}).get("root", symbol).upper()
        today = now_naive().date()
        seen = set()
        for exchange in ("NFO", "BFO"):
            for row in self._instruments.get(exchange, []):
                if str(row.get("instrument_type", "")).upper() not in ("CE", "PE"):
                    continue
                if str(row.get("name", "")).upper() != root:
                    continue
                expiry = _as_date(row.get("expiry"))
                if expiry and expiry >= today:
                    seen.add(expiry)
        return [{"date": d, "epoch_ms": int(datetime.combine(d, datetime.min.time()).timestamp() * 1000)}
                for d in sorted(seen)]

    async def get_option_quote(self, symbol: str, expiry: Dict, strike: int,
                               opt_type: str) -> Optional[Dict]:
        """
        One strike, with bid/ask depth. Kite has no option chain endpoint, so
        the contract is located in the instruments dump and then quoted.
        """
        self._require_connection()
        root = INDEX_LOOKUP.get(symbol, {}).get("root", symbol).upper()
        want_expiry = expiry.get("date")
        want_type = str(opt_type).upper()

        contract = None
        for exchange in ("NFO", "BFO"):
            for row in self._instruments.get(exchange, []):
                if str(row.get("instrument_type", "")).upper() != want_type:
                    continue
                if str(row.get("name", "")).upper() != root:
                    continue
                if _as_date(row.get("expiry")) != want_expiry:
                    continue
                if abs(float(row.get("strike") or 0) - strike) > 0.51:
                    continue
                contract = row
                break
            if contract:
                break
        if not contract:
            return None

        key = f"{contract['exchange']}:{contract['tradingsymbol']}"
        try:
            data = await self._call(lambda: self.kite.quote([key]),
                                    f"option {contract['tradingsymbol']}")
        except Exception as exc:
            logger.warning("[Kite] Option quote failed for %s: %s", key, exc)
            return None

        row = (data or {}).get(key)
        if not row:
            return None

        depth = row.get("depth") or {}
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        bid = float(bids[0]["price"]) if bids and bids[0].get("price") else 0.0
        ask = float(asks[0]["price"]) if asks and asks[0].get("price") else 0.0
        ltp = float(row.get("last_price") or 0)

        # Prefer the mid when both sides are quoted. A stale last-trade on a
        # wide-spread option corrupts the solved IV, which then corrupts the
        # IV-rank gate that depends on it.
        price = (bid + ask) / 2 if bid > 0 and ask > 0 else ltp
        if price <= 0:
            return None

        spread = (ask - bid) if bid > 0 and ask > 0 else None
        return {
            "ltp": price,
            "last_price": ltp,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pct": round(spread / price * 100, 2) if spread and price else None,
            "oi": float(row.get("oi") or 0),
            "volume": float(row.get("volume") or row.get("volume_traded") or 0),
            "scrip_code": int(contract["instrument_token"]),
            "tradingsymbol": contract["tradingsymbol"],
            "lot_size": int(contract.get("lot_size") or 0),
        }

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #
    def start_stream(self, symbols: List[str]) -> bool:
        if not cfg.USE_WEBSOCKET:
            return False
        if self.stream and self.stream.connected:
            return True
        self._require_connection()
        if KiteTicker is None:
            logger.error("[WS] kiteconnect is not installed")
            return False

        token_map: Dict[str, int] = {}
        for symbol in symbols:
            try:
                token_map[symbol] = int(self._instrument(symbol, True)["instrument_token"])
            except KeyError:
                logger.warning("[WS] No instrument for %s - not streamed", symbol)

        if not token_map:
            logger.error("[WS] Nothing to subscribe to")
            return False

        minutes = 5 if cfg.TIMEFRAME == "5m" else 15
        self.stream = KiteTickStream(cfg.KITE_API_KEY, self.kite.access_token,
                                     token_map, minutes=minutes)
        self.stream.start()
        logger.info("[WS] Streaming %d instrument(s) at %dm bars", len(token_map), minutes)
        return True

    def stop_stream(self) -> None:
        if self.stream:
            self.stream.stop()
            logger.info("[WS] Stream stopped")

    def stream_healthy(self) -> bool:
        return bool(self.stream and self.stream.connected and not self.stream.is_stale())

    # ------------------------------------------------------------------ #
    def _require_connection(self):
        if not self.connected or self.kite is None:
            raise ConnectionError("Kite feed is not connected - call connect() first")

    async def _call(self, fn, label: str, attempts: int = 3, base_delay: float = 1.0):
        last = None
        for i in range(attempts):
            try:
                await self._limiter.acquire()
                async with self._sem:
                    return await asyncio.to_thread(fn)
            except Exception as exc:
                last = exc
                self.last_error = f"{label}: {exc}"
                text = str(exc).lower()
                # An expired token will never succeed on retry.
                if "token" in text and ("expire" in text or "invalid" in text):
                    self.connected = False
                    raise CredentialsError(
                        f"{label} failed - the Kite token has expired. Open /kite/login.")
                if i < attempts - 1:
                    delay = base_delay * (2 ** i)
                    logger.warning("[Kite] %s failed (%s) - retrying in %.0fs", label, exc, delay)
                    await asyncio.sleep(delay)
        raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


# --------------------------------------------------------------------------- #
class KiteTickStream:
    """
    KiteTicker wrapper producing the same interface the candle store expects:
    `.builders`, `.connected`, `.is_stale()`, `.unsubscribe()`, `.status()`.

    KiteTicker runs its own thread via connect(threaded=True) and handles
    reconnection internally, so there is no hand-rolled socket loop here.
    """

    def __init__(self, api_key: str, access_token: str,
                 token_map: Dict[str, int], minutes: int = 5):
        from feed_ws import BarBuilder

        self.token_map = token_map
        self.by_token = {v: k for k, v in token_map.items()}
        self.minutes = minutes
        self.builders = {sym: BarBuilder(sym, minutes) for sym in token_map}
        self.ltp: Dict[str, float] = {}
        self.oi: Dict[str, float] = {}
        self.connected = False
        self.subscribed = False
        self.tick_count = 0
        self.last_message_at: Optional[datetime] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()

        self.ticker = KiteTicker(api_key, access_token)
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error

    def start(self) -> None:
        self.ticker.connect(threaded=True)

    def _on_connect(self, ws, response):
        tokens = list(self.token_map.values())
        ws.subscribe(tokens)
        # QUOTE mode carries last price, cumulative volume and OI, which is
        # everything the bar builder and the OI filter need. FULL adds market
        # depth per tick and far more bandwidth for no benefit here.
        ws.set_mode(ws.MODE_QUOTE, tokens)
        self.connected = True
        self.subscribed = True
        logger.info("[WS] Connected, subscribed to %d token(s)", len(tokens))

    def _on_ticks(self, ws, ticks):
        stamp = now_naive()
        with self._lock:
            self.last_message_at = stamp
            for tick in ticks or []:
                symbol = self.by_token.get(tick.get("instrument_token"))
                if not symbol:
                    continue
                price = float(tick.get("last_price") or 0)
                if price <= 0:
                    continue
                self.tick_count += 1
                self.ltp[symbol] = price
                if tick.get("oi") is not None:
                    self.oi[symbol] = float(tick["oi"])
                # volume_traded is cumulative for the day; BarBuilder converts
                # it to a per-bar figure by differencing.
                cum_volume = tick.get("volume_traded")
                ts = tick.get("exchange_timestamp") or tick.get("last_trade_time") or stamp
                if isinstance(ts, datetime) and ts.tzinfo is not None:
                    ts = ts.replace(tzinfo=None)
                builder = self.builders.get(symbol)
                if builder:
                    builder.add_tick(price, float(cum_volume) if cum_volume is not None else None, ts)

    def _on_close(self, ws, code, reason):
        self.connected = False
        logger.warning("[WS] Closed (%s): %s", code, reason)

    def _on_error(self, ws, code, reason):
        self.error = f"{code}: {reason}"
        logger.error("[WS] Error %s", self.error)

    # ------------------------------------------------------------------ #
    def unsubscribe(self, symbols: List[str] = None) -> bool:
        """Stop consuming the feed once the daily signal cap is reached."""
        targets = symbols or list(self.token_map)
        tokens = [self.token_map[s] for s in targets if s in self.token_map]
        if not tokens:
            return False
        try:
            self.ticker.unsubscribe(tokens)
            self.subscribed = False
            logger.info("[WS] Unsubscribed from %d token(s)", len(tokens))
            return True
        except Exception as exc:
            logger.error("[WS] Unsubscribe failed: %s", exc)
            return False

    def stop(self) -> None:
        try:
            self.ticker.close()
        except Exception:
            pass
        self.connected = False
        self.subscribed = False

    def is_stale(self) -> bool:
        if not self.connected or self.last_message_at is None:
            return True
        return (now_naive() - self.last_message_at).total_seconds() > cfg.WS_STALE_SECONDS

    def frame(self, symbol: str):
        with self._lock:
            builder = self.builders.get(symbol)
            return builder.frame() if builder else None

    def status(self) -> Dict:
        return {
            "broker": "kite",
            "connected": self.connected,
            "subscribed": self.subscribed,
            "instruments": len(self.token_map),
            "ticks": self.tick_count,
            "stale": self.is_stale(),
            "last_message": self.last_message_at.isoformat() if self.last_message_at else None,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
def _quote_from(row: Dict) -> Dict:
    ohlc = row.get("ohlc") or {}
    return {
        "ltp": float(row.get("last_price") or 0),
        "open": float(ohlc.get("open") or 0),
        "high": float(ohlc.get("high") or 0),
        "low": float(ohlc.get("low") or 0),
        "prev_close": float(ohlc.get("close") or 0),
        "volume": float(row.get("volume") or row.get("volume_traded") or 0),
        "oi": float(row.get("oi") or 0),
    }


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(text.split("T")[0].split(" ")[0]
                                     if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None
