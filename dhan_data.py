"""
DhanHQ v2 historical data for backtests (15 Nov 2026).

Kite has no expired-options history and only ~6 months of 5-minute spot data
in our store; Dhan serves 5 years of intraday candles for active instruments
(stocks, indices) and expired options by strike relative to ATM. This module
is used by the backtest tools only - live scanning stays on Kite.

Login, in order (same scheme as the Index Average project):
  1. DHAN_ACCESS_TOKEN, if set and accepted;
  2. a token cached in broker_tokens (broker="dhan") from the last 20 hours;
  3. a new token from DHAN_CLIENT_ID + DHAN_PIN + DHAN_TOTP_SECRET.
Dhan allows up to 5 active tokens per account, so this does not log out other
programs using the same account.

Rate limit: Dhan data APIs allow 5 requests/second; we stay at 4.
"""
import csv
import io
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd

from clock import now_naive

logger = logging.getLogger(__name__)

API = "https://api.dhan.co/v2"
AUTH = "https://auth.dhan.co"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
AUTH_ERRORS = {"DH-901", "DH-902", "807", "808", "809", "810"}
RATE_ERRORS = {"DH-904", "805"}
IST = timedelta(hours=5, minutes=30)

# Our symbol -> candidate Dhan index trading symbols (segment I in the master).
INDEX_ALIASES = {
    "NIFTY 50": ["NIFTY", "NIFTY 50"],
    "BANKNIFTY": ["BANKNIFTY", "NIFTY BANK"],
    "NIFTY BANK": ["BANKNIFTY", "NIFTY BANK"],
    "FINNIFTY": ["FINNIFTY", "NIFTY FIN SERVICE"],
    "MIDCPNIFTY": ["MIDCPNIFTY", "NIFTY MID SELECT"],
    "SENSEX": ["SENSEX"],
    "INDIA VIX": ["INDIA VIX", "INDIAVIX"],
}


class DhanError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code, self.status = code, status

    @property
    def is_auth(self) -> bool:
        return self.code in AUTH_ERRORS or self.status == 401


def epochs_to_ist(epochs, intraday: bool) -> List[datetime]:
    """Dhan chart timestamps are epoch seconds (UTC). Returned as naive IST.
    Guard (as in the Index Average project): if intraday candles start after
    14:30 the epochs were already IST-shifted, so shift back."""
    out = [datetime.utcfromtimestamp(float(t)) + IST for t in epochs]
    if intraday and out and out[0].hour * 60 + out[0].minute >= 14 * 60 + 30:
        out = [t - IST for t in out]
    return out


class DhanData:
    def __init__(self, client_id: str = None, pin: str = None, totp_secret: str = None,
                 access_token: str = None, http: httpx.Client = None, sleep=time.sleep,
                 per_sec: float = 4.0):
        self.client_id = client_id or os.getenv("DHAN_CLIENT_ID", "")
        self.pin = pin or os.getenv("DHAN_PIN", "")
        self.totp_secret = totp_secret or os.getenv("DHAN_TOTP_SECRET", "")
        self.env_token = access_token or os.getenv("DHAN_ACCESS_TOKEN", "")
        self.http = http or httpx.Client(timeout=30)
        self.sleep = sleep
        self.gap = 1.0 / per_sec
        self._next = 0.0
        self.token = ""
        self.calls = 0
        self.profile: Dict = {}

    # ------------------------------------------------------------- login
    def login(self) -> None:
        if not self.client_id:
            raise DhanError("DHAN_CLIENT_ID is not set", "login")
        tried = []
        for source, token in (("DHAN_ACCESS_TOKEN", self.env_token), ("cached", self._cached())):
            if token:
                self.token = token
                if self._works():
                    logger.info("[Dhan] Login OK via %s (data plan: %s)", source,
                                self.profile.get("dataPlan", "?"))
                    return
                tried.append(f"{source} rejected")
        if self.pin and self.totp_secret:
            self._generate()
            if self._works():
                logger.info("[Dhan] Login OK via PIN + TOTP (data plan: %s)", self.profile.get("dataPlan", "?"))
                return
            tried.append("new token rejected")
        else:
            tried.append("no DHAN_PIN / DHAN_TOTP_SECRET")
        raise DhanError("Dhan login failed: " + "; ".join(tried), "login")

    def _works(self) -> bool:
        try:
            self.profile = self._call("GET", "/profile", retries=1) or {}
            return True
        except DhanError as exc:
            logger.warning("[Dhan] token check failed: %s", exc)
            return False

    def _cached(self) -> Optional[str]:
        try:
            from database import BrokerToken, session_scope
            with session_scope() as db:
                row = (db.query(BrokerToken).filter(BrokerToken.broker == "dhan",
                                                    BrokerToken.user_id == self.client_id)
                       .order_by(BrokerToken.issued_at.desc()).first())
                if row and row.issued_at and row.issued_at > now_naive() - timedelta(hours=20):
                    return row.access_token
        except Exception as exc:
            logger.debug("[Dhan] no cached token: %s", exc)
        return None

    def _generate(self) -> None:
        import pyotp
        totp = pyotp.TOTP(self.totp_secret)
        if totp.interval - time.time() % totp.interval < 3:   # code about to roll over
            self.sleep(3.5)
        r = self.http.post(f"{AUTH}/app/generateAccessToken",
                           params={"dhanClientId": self.client_id, "pin": self.pin, "totp": totp.now()})
        try:
            body = r.json()
        except ValueError:
            body = {}
        token = body.get("accessToken") if isinstance(body, dict) else None
        if r.status_code >= 300 or not token:
            msg = (body.get("message") or body.get("errorMessage")) if isinstance(body, dict) else ""
            raise DhanError(f"TOTP token generation failed ({r.status_code}): {msg or r.text[:150]}",
                            "login", r.status_code)
        self.token = token
        try:
            from database import BrokerToken, session_scope
            with session_scope() as db:
                db.query(BrokerToken).filter(BrokerToken.broker == "dhan").delete(synchronize_session=False)
                db.add(BrokerToken(broker="dhan", access_token=token, user_id=self.client_id))
        except Exception as exc:
            logger.warning("[Dhan] could not cache token: %s", exc)

    # -------------------------------------------------------------- http
    def _call(self, method: str, path: str, body: Dict = None, retries: int = 4):
        last = None
        for attempt in range(retries):
            now = time.monotonic()
            if now < self._next:
                self.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap
            self.calls += 1
            try:
                r = self.http.request(method, API + path, json=body, headers={
                    "access-token": self.token, "client-id": self.client_id,
                    "Content-Type": "application/json", "Accept": "application/json"})
            except httpx.HTTPError as exc:
                last = DhanError(f"network error on {path}: {exc}", "net")
                self.sleep(min(2 ** attempt, 8))
                continue
            try:
                data = r.json() if r.content else None
            except ValueError:
                data = None
            if 200 <= r.status_code < 300 and not (isinstance(data, dict) and data.get("status") == "failure"):
                return data
            code, msg = self._error(data, r)
            last = DhanError(f"{method} {path}: {msg}", code, r.status_code)
            if code in RATE_ERRORS or r.status_code == 429 or r.status_code >= 500:
                self.sleep(1.0 + attempt * 1.5)
                continue
            raise last
        raise last or DhanError(f"{method} {path} failed")

    @staticmethod
    def _error(data, r) -> Tuple[str, str]:
        if isinstance(data, dict):
            if data.get("errorCode") or data.get("errorMessage"):
                return str(data.get("errorCode", "")), str(data.get("errorMessage") or data.get("errorType"))
            rem = data.get("remarks")
            if isinstance(rem, dict):
                return str(rem.get("error_code", "")), str(rem.get("error_message") or rem.get("error_type"))
            if rem:
                return "", str(rem)
        return str(r.status_code), (r.text or "")[:200]

    # ----------------------------------------------------------- candles
    @staticmethod
    def _frame(data, intraday: bool) -> pd.DataFrame:
        d = data or {}
        if isinstance(d.get("data"), dict):
            d = d["data"]
        ts = d.get("timestamp") or d.get("start_Time") or []
        cols = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
        if not ts:
            return pd.DataFrame(columns=cols)
        n = len(ts)
        oi = d.get("open_interest") or d.get("oi") or [None] * n
        out = pd.DataFrame({
            "timestamp": epochs_to_ist(ts, intraday),
            "open": d.get("open", [None] * n), "high": d.get("high", [None] * n),
            "low": d.get("low", [None] * n), "close": d.get("close", [None] * n),
            "volume": d.get("volume", [0] * n), "oi": list(oi)[:n] + [None] * max(0, n - len(oi)),
        })
        out = out.dropna(subset=["open", "high", "low", "close"])
        if not intraday:
            out["timestamp"] = pd.to_datetime(out["timestamp"]).dt.normalize()
        out["oi"] = out["oi"].where(out["oi"].astype(float) > 0) if len(out) else out["oi"]
        return out[cols].reset_index(drop=True)

    def intraday(self, security_id: str, segment: str, instrument: str, start: datetime,
                 end: datetime, interval: int = 5, oi: bool = False) -> pd.DataFrame:
        """Up to 90 days per call (Dhan limit)."""
        data = self._call("POST", "/charts/intraday", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
            "interval": str(int(interval)), "oi": bool(oi),
            "fromDate": start.strftime("%Y-%m-%d %H:%M:%S"), "toDate": end.strftime("%Y-%m-%d %H:%M:%S")})
        return self._frame(data, intraday=True)

    def daily(self, security_id: str, segment: str, instrument: str, start: date, end: date,
              oi: bool = False) -> pd.DataFrame:
        data = self._call("POST", "/charts/historical", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
            "expiryCode": 0, "oi": bool(oi), "fromDate": start.isoformat(), "toDate": end.isoformat()})
        return self._frame(data, intraday=False)

    # ------------------------------------------------------ instrument map
    def instrument_map(self, symbols: List[str], text: str = None) -> Dict[str, Tuple[str, str, str]]:
        """{our symbol: (security_id, exchangeSegment, instrument)} for stocks (NSE EQ) and indices."""
        if text is None:
            r = self.http.get(MASTER_URL, timeout=120)
            r.raise_for_status()
            text = r.text
        rd = csv.DictReader(io.StringIO(text))
        eq, idx = {}, {}
        for row in rd:
            exch = (row.get("SEM_EXM_EXCH_ID") or "").strip().upper()
            seg = (row.get("SEM_SEGMENT") or "").strip().upper()
            sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
            sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
            if not sym or not sid:
                continue
            if exch == "NSE" and seg == "E" and (row.get("SEM_SERIES") or "EQ").strip().upper() == "EQ":
                eq.setdefault(sym, sid)
            elif seg == "I" and exch in ("NSE", "BSE"):
                idx.setdefault(sym, sid)
                name = (row.get("SM_SYMBOL_NAME") or row.get("SEM_CUSTOM_SYMBOL") or "").strip().upper()
                if name:
                    idx.setdefault(name, sid)
        out = {}
        for s in symbols:
            key = s.upper()
            if key in INDEX_ALIASES or key in idx and key not in eq:
                for cand in INDEX_ALIASES.get(key, [key]):
                    if cand in idx:
                        out[s] = (idx[cand], "IDX_I", "INDEX")
                        break
            elif key in eq:
                out[s] = (eq[key], "NSE_EQ", "EQUITY")
        return out
