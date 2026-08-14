"""
5paisa market data feed.

The py5paisa SDK is synchronous and swallows its own exceptions (returning None
on failure), so every call is wrapped in asyncio.to_thread plus an explicit
None/shape check. Signatures below match py5paisa 0.7.x:

    historical_data(Exch, ExchangeSegment, ScripCode, time, From, To)  -> positional
    fetch_market_feed(req_list)                                        -> dict body
    get_totp_session(client_code, totp, pin)                           -> auth
    get_scrips()                                                       -> scrip master df

Verify against your installed version with `python -c "import py5paisa, inspect;
print(inspect.signature(py5paisa.FivePaisaClient.historical_data))"` if 5paisa
ships a breaking change.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import cfg
from feed_base import MarketFeed, RateLimiter

try:
    from py5paisa import FivePaisaClient
except ImportError:  # allows the app to boot in mock mode without the SDK
    FivePaisaClient = None

try:
    import pyotp
except ImportError:
    pyotp = None

logger = logging.getLogger(__name__)


class FivePaisaFeed(MarketFeed):
    name = "5paisa"

    # Cash index tokens. These are stable but worth confirming against the
    # scrip master once - a wrong token silently returns someone else's candles.
    INDEX_CASH_SCRIPS = {
        "NIFTY 50":  {"Exch": "N", "ExchType": "C", "ScripCode": 999920000},
        "BANKNIFTY": {"Exch": "N", "ExchType": "C", "ScripCode": 999920005},
        "FINNIFTY":  {"Exch": "N", "ExchType": "C", "ScripCode": 999920037},
        "SENSEX":    {"Exch": "B", "ExchType": "C", "ScripCode": 999901},
    }
    # Populated at startup by ScripResolver - futures carry the volume and OI
    # that cash indices don't publish.
    INDEX_FUTURES_SCRIPS: Dict[str, Dict] = {}

    VALID_INTERVALS = {"1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d"}
    BARS_PER_DAY = {"1m": 375, "3m": 125, "5m": 75, "10m": 38, "15m": 25, "30m": 13, "60m": 7, "1d": 1}

    def __init__(self):
        self.client = None
        self.connected = False
        self.last_error: Optional[str] = None
        self._quote_cache: Dict[str, Dict] = {}
        self._sem = asyncio.Semaphore(cfg.MAX_CONCURRENT_FETCHES)
        self._limiter = RateLimiter(int(cfg.API_CALLS_PER_SECOND), 1.0)
        self._chain_cache: Dict = {}
        self._chain_cache_time: Dict = {}
        self.resolver = None

    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        if FivePaisaClient is None:
            raise RuntimeError("py5paisa is not installed. pip install py5paisa")

        cred = {
            "APP_NAME": cfg.FIVEPAISA_APP_NAME,
            "APP_SOURCE": cfg.FIVEPAISA_APP_SOURCE,
            "USER_ID": cfg.FIVEPAISA_USER_ID,
            "PASSWORD": cfg.FIVEPAISA_PASSWORD,
            "USER_KEY": cfg.FIVEPAISA_USER_KEY,
            "ENCRYPTION_KEY": cfg.FIVEPAISA_ENCRYPTION_KEY,
        }

        def _login():
            client = FivePaisaClient(cred=cred)
            totp = cfg.FIVEPAISA_TOTP
            if not totp and cfg.FIVEPAISA_TOTP_SECRET:
                if pyotp is None:
                    raise RuntimeError("pyotp is required to generate TOTP codes. pip install pyotp")
                totp = pyotp.TOTP(cfg.FIVEPAISA_TOTP_SECRET.replace(" ", "")).now()
            if not totp:
                raise RuntimeError("Set FIVEPAISA_TOTP_SECRET (preferred) or FIVEPAISA_TOTP")

            client.get_totp_session(cfg.FIVEPAISA_CLIENT_CODE, totp, cfg.FIVEPAISA_PIN)
            token = getattr(client, "access_token", "")
            if not token:
                raise RuntimeError("5paisa login failed - no access token returned. "
                                   "Check client code, PIN, TOTP seed and API key status.")
            return client

        self.client = await asyncio.to_thread(_login)
        self.connected = True
        self.last_error = None
        logger.info("[5paisa] Authenticated as %s", cfg.FIVEPAISA_CLIENT_CODE)
        await self._load_futures_map()
        return True

    async def _load_futures_map(self):
        from scrip_resolver import ScripResolver
        self.resolver = ScripResolver(self.client)
        try:
            resolved = await self.resolver.refresh_all(force=False)
            self.INDEX_FUTURES_SCRIPS.update(resolved)
            logger.info("[5paisa] %d futures contracts mapped", len(resolved))
        except Exception as exc:
            logger.error("[5paisa] Futures mapping failed, falling back to cash tokens: %s", exc)

    # ------------------------------------------------------------------ #
    def _scrip(self, symbol: str, use_futures: bool) -> Dict:
        if use_futures and symbol in self.INDEX_FUTURES_SCRIPS:
            return self.INDEX_FUTURES_SCRIPS[symbol]
        if symbol in self.INDEX_CASH_SCRIPS:
            return self.INDEX_CASH_SCRIPS[symbol]
        raise KeyError(f"No scrip mapping for '{symbol}'. Add it to INDEX_CASH_SCRIPS "
                       f"or ScripResolver.ROOT_MAP.")

    def _require_connection(self):
        if not self.connected or self.client is None:
            raise ConnectionError("5paisa feed is not connected - call connect() first")

    # ------------------------------------------------------------------ #
    async def get_historical(self, symbol: str, interval: str = "5m", bars: int = 200,
                             use_futures: bool = True, from_date=None) -> pd.DataFrame:
        self._require_connection()
        if interval not in self.VALID_INTERVALS:
            raise ValueError(f"Unsupported interval '{interval}'. Allowed: {sorted(self.VALID_INTERVALS)}")

        scrip = self._scrip(symbol, use_futures)
        to_date = datetime.now()
        if from_date is None:
            per_day = self.BARS_PER_DAY.get(interval, 75)
            # Pad for weekends and holidays so we actually come back with `bars`.
            days = max(7, int(np.ceil(bars / per_day) * 1.8) + 3)
            from_date = to_date - timedelta(days=days)
        elif not isinstance(from_date, datetime):
            from_date = datetime.combine(from_date, datetime.min.time())

        def _fetch():
            return self.client.historical_data(
                str(scrip["Exch"]),
                str(scrip["ExchType"]),
                int(scrip["ScripCode"]),
                interval,
                from_date.strftime("%Y-%m-%d"),
                to_date.strftime("%Y-%m-%d"),
            )

        async with self._sem:
            raw = await self._retry(_fetch, f"historical {symbol} {interval}")

        if isinstance(raw, str):           # SDK returns an error string, not an exception
            raise ValueError(f"5paisa rejected the request for {symbol}: {raw}")
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            raise ValueError(f"No candles returned for {symbol} {interval} "
                             f"(scrip {scrip['ScripCode']}, futures={use_futures})")

        df = self.normalize(pd.DataFrame(raw), bars)
        logger.debug("[5paisa] %s %s -> %d bars", symbol, interval, len(df))
        return df

    async def get_live_quote(self, symbol: str, use_futures: bool = False) -> Dict:
        self._require_connection()
        scrip = self._scrip(symbol, use_futures)
        req = [{"Exch": scrip["Exch"], "ExchType": scrip["ExchType"], "ScripCode": int(scrip["ScripCode"])}]

        def _fetch():
            return self.client.fetch_market_feed(req)

        try:
            async with self._sem:
                body = await self._retry(_fetch, f"quote {symbol}")
            row = self._first_row(body)
            if not row:
                raise ValueError("empty market feed payload")

            quote = {
                "ltp": _num(row, ["LastRate", "LastTradedPrice", "LTP"]),
                "open": _num(row, ["Open", "OpenRate"]),
                "high": _num(row, ["High", "HighRate"]),
                "low": _num(row, ["Low", "LowRate"]),
                "prev_close": _num(row, ["PClose", "PrevClose", "Close"]),
                "volume": _num(row, ["TotalQty", "Volume"]),
                "oi": _num(row, ["OpenInterest", "OI"]),
            }
            if quote["ltp"] <= 0:
                raise ValueError(f"market feed returned ltp={quote['ltp']}")
            self._quote_cache[symbol] = quote
            return quote
        except Exception as exc:
            if symbol in self._quote_cache:
                logger.warning("[5paisa] Quote failed for %s (%s) - serving cached", symbol, exc)
                return self._quote_cache[symbol]
            raise

    async def get_oi(self, symbol: str) -> Optional[float]:
        """OI on the futures contract. Returns None rather than a made-up number."""
        try:
            quote = await self.get_live_quote(symbol, use_futures=True)
            oi = float(quote.get("oi") or 0)
            return oi if oi > 0 else None
        except Exception as exc:
            logger.warning("[5paisa] OI unavailable for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------ #
    async def get_fno_stocks(self) -> List[Dict]:
        """
        Stock futures from the scrip master, nearest expiry per underlying.
        Index futures are excluded - those are tracked on the index path.
        """
        self._require_connection()
        from scrip_resolver import ScripResolver

        resolver = ScripResolver(self.client)
        df = await resolver._fetch_master()
        if df is None or getattr(df, "empty", True):
            logger.error("[5paisa] Scrip master unavailable for universe build")
            return []
        df = resolver._standardize(df)
        if df is None:
            return []

        mask = (df["exch"].astype(str).str.upper() == "N") & \
               (df["exchtype"].astype(str).str.upper() == "D")
        deriv = df[mask].copy()
        if deriv.empty:
            return []

        if "scriptype" in deriv.columns:
            deriv = deriv[deriv["scriptype"].astype(str).str.upper().str.strip()
                          .isin(["XX", "FUT", "FUTSTK", "FUTIDX"])]
        else:
            names = deriv["name"].astype(str).str.upper()
            deriv = deriv[~names.str.contains(r"(?:\d+\s*(?:CE|PE)\b)|(?:\s(?:CE|PE)$)",
                                              regex=True, na=False)]
        if deriv.empty:
            return []

        root_col = "root" if "root" in deriv.columns else "name"
        deriv["_root"] = deriv[root_col].astype(str).str.upper().str.strip()
        index_roots = {m["root"].upper() for m in ScripResolver.ROOT_MAP.values()}
        index_roots |= {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
        deriv = deriv[~deriv["_root"].isin(index_roots)]
        if deriv.empty:
            return []

        deriv["_expiry"] = deriv.apply(resolver._expiry_of, axis=1)
        today = datetime.now().date()
        deriv = deriv[deriv["_expiry"].notna()]
        deriv = deriv[deriv["_expiry"] >= today]
        if deriv.empty:
            return []

        deriv = deriv.sort_values(["_root", "_expiry"]).drop_duplicates("_root", keep="first")

        out = []
        for row in deriv.itertuples(index=False):
            data = row._asdict()
            try:
                code = int(float(data.get("scripcode")))
            except (TypeError, ValueError):
                continue
            out.append({
                "symbol": str(data.get("_root")),
                "root": str(data.get("_root")),
                "scrip_code": code,
                "exch": str(data.get("exch", "N")).upper(),
                "exch_type": str(data.get("exchtype", "D")).upper(),
                "lot_size": _safe_int_local(data.get("lotsize", 0)),
                "expiry": data["_expiry"].strftime("%Y-%m-%d"),
            })
        logger.info("[5paisa] %d stock futures found in the scrip master", len(out))
        return out

    async def get_bulk_quotes(self, instruments: List[Dict]) -> Dict[str, Dict]:
        """
        fetch_market_feed() accepts a list, so many scrips cost one call.
        Chunked because oversized payloads are rejected rather than truncated.
        """
        self._require_connection()
        out: Dict[str, Dict] = {}
        size = max(1, cfg.QUOTE_BATCH_SIZE)
        chunks = [instruments[i:i + size] for i in range(0, len(instruments), size)]

        for chunk in chunks:
            req = [{"Exch": c["exch"], "ExchType": c["exch_type"],
                    "ScripCode": int(c["scrip_code"])} for c in chunk]
            by_token = {int(c["scrip_code"]): c["symbol"] for c in chunk}

            def _fetch(payload=req):
                return self.client.fetch_market_feed(payload)

            try:
                async with self._sem:
                    body = await self._retry(_fetch, f"bulk quotes x{len(req)}")
            except Exception as exc:
                logger.warning("[5paisa] Bulk quote chunk failed: %s", exc)
                continue

            rows = []
            if isinstance(body, dict):
                for key in ("Data", "data", "MarketFeedData"):
                    if isinstance(body.get(key), list):
                        rows = body[key]
                        break
            elif isinstance(body, list):
                rows = body

            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = row.get("Token") or row.get("ScripCode")
                try:
                    symbol = by_token.get(int(token))
                except (TypeError, ValueError):
                    continue
                if not symbol:
                    continue
                out[symbol] = {
                    "ltp": _num(row, ["LastRate", "LastTradedPrice", "LTP"]),
                    "volume": _num(row, ["TotalQty", "Volume"]),
                    "prev_close": _num(row, ["PClose", "PrevClose", "Close"]),
                    "high": _num(row, ["High", "HighRate"]),
                    "low": _num(row, ["Low", "LowRate"]),
                }

        logger.info("[5paisa] Quoted %d/%d instruments in %d call(s)",
                    len(out), len(instruments), len(chunks))
        return out

    async def get_expiries(self, symbol: str) -> List[Dict]:
        """
        py5paisa: get_expiry(exch, symbol) -> body carrying /Date(ms)/ strings.
        The epoch value is fed straight back into get_option_chain.
        """
        self._require_connection()
        from scrip_resolver import ScripResolver
        meta = ScripResolver.ROOT_MAP.get(symbol)
        if not meta:
            raise KeyError(f"No option root mapped for '{symbol}'")

        def _fetch():
            return self.client.get_expiry(meta["exch"], meta["root"])

        async with self._sem:
            body = await self._retry(_fetch, f"expiries {symbol}")

        rows = []
        if isinstance(body, dict):
            for key in ("Expiry", "ExpiryDates", "Data"):
                if isinstance(body.get(key), list):
                    rows = body[key]
                    break
        elif isinstance(body, list):
            rows = body

        out = []
        for row in rows:
            raw = row.get("ExpiryDate") if isinstance(row, dict) else row
            parsed = _parse_dotnet_date(raw)
            if parsed:
                out.append(parsed)
        out.sort(key=lambda e: e["date"])
        return out

    async def get_option_quote(self, symbol: str, expiry: Dict, strike: int,
                               opt_type: str) -> Optional[Dict]:
        """Pull the chain for one expiry and pick out a single strike."""
        self._require_connection()
        from scrip_resolver import ScripResolver
        meta = ScripResolver.ROOT_MAP.get(symbol)
        if not meta:
            raise KeyError(f"No option root mapped for '{symbol}'")

        cache_key = (symbol, expiry.get("epoch_ms"))
        chain = self._chain_cache.get(cache_key)
        fetched_at = self._chain_cache_time.get(cache_key)
        stale = (fetched_at is None or
                 (datetime.now() - fetched_at).total_seconds() > 60)

        if chain is None or stale:
            def _fetch():
                return self.client.get_option_chain(meta["exch"], meta["root"],
                                                    int(expiry["epoch_ms"]))

            async with self._sem:
                body = await self._retry(_fetch, f"chain {symbol}")

            rows = []
            if isinstance(body, dict):
                for key in ("Options", "Data", "OptionChain"):
                    if isinstance(body.get(key), list):
                        rows = body[key]
                        break
            elif isinstance(body, list):
                rows = body
            chain = rows
            self._chain_cache[cache_key] = rows
            self._chain_cache_time[cache_key] = datetime.now()

        want = str(opt_type).upper()
        for row in chain:
            if not isinstance(row, dict):
                continue
            cp = str(row.get("CPType") or row.get("OptionType") or "").upper()
            if cp and cp != want:
                continue
            try:
                row_strike = float(row.get("StrikeRate", row.get("Strike", 0)))
            except (TypeError, ValueError):
                continue
            if abs(row_strike - strike) > 0.51:
                continue
            ltp = _num(row, ["LastRate", "LastTradedPrice", "LTP"])
            if ltp <= 0:
                # An untraded strike quotes zero. Fall back to the mid.
                bid = _num(row, ["Bid", "BidRate"])
                ask = _num(row, ["Ask", "AskRate", "Offer"])
                ltp = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            if ltp <= 0:
                return None
            return {
                "ltp": ltp,
                "oi": _num(row, ["OpenInterest", "OI"]),
                "volume": _num(row, ["Volume", "TotalQty"]),
                "scrip_code": int(_num(row, ["ScripCode", "Token"]) or 0) or None,
            }
        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _first_row(body) -> Optional[Dict]:
        if body is None:
            return None
        if isinstance(body, dict):
            for key in ("Data", "data", "MarketFeedData"):
                rows = body.get(key)
                if isinstance(rows, list) and rows:
                    return rows[0]
            return body if "LastRate" in body else None
        if isinstance(body, list) and body:
            return body[0]
        return None

    async def _retry(self, fn, label: str, attempts: int = 3, base_delay: float = 1.0):
        last = None
        for i in range(attempts):
            try:
                await self._limiter.acquire()
                return await asyncio.to_thread(fn)
            except Exception as exc:
                last = exc
                self.last_error = f"{label}: {exc}"
                if i < attempts - 1:
                    delay = base_delay * (2 ** i)
                    logger.warning("[5paisa] %s failed (%s) - retrying in %.0fs", label, exc, delay)
                    await asyncio.sleep(delay)
        raise RuntimeError(f"{label} failed after {attempts} attempts: {last}")


def _safe_int_local(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_dotnet_date(raw) -> Optional[Dict]:
    """5paisa hands back /Date(1712345678000)/ - sometimes a plain date."""
    if raw is None:
        return None
    text = str(raw)
    match = re.search(r"/Date\((-?\d+)", text)
    if match:
        epoch_ms = int(match.group(1))
        try:
            return {"date": datetime.fromtimestamp(epoch_ms / 1000).date(),
                    "epoch_ms": epoch_ms}
        except (ValueError, OSError):
            return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            parsed = datetime.strptime(text.split()[0], fmt)
            return {"date": parsed.date(), "epoch_ms": int(parsed.timestamp() * 1000)}
        except ValueError:
            continue
    return None


def _num(row: Dict, keys: List[str]) -> float:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return 0.0
