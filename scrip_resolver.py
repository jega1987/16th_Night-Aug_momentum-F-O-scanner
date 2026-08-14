"""
Resolves index futures scrip codes from the 5paisa scrip master.

Futures codes change every expiry, so a hardcoded number quietly starts
returning an expired contract's candles. This pulls the master, picks the
nearest non-expired future per index, caches it in the database, and can be
re-run on expiry day.

py5paisa exposes the master as `client.get_scrips()` (a CSV parsed into a
DataFrame). Column names vary between releases, so everything below matches
case-insensitively with fallbacks.
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, Optional

import pandas as pd

from clock import now_naive, today_start
from database import ScripMapping, session_scope

logger = logging.getLogger(__name__)

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


class ScripResolver:
    ROOT_MAP = {
        "NIFTY 50":  {"root": "NIFTY",     "exch": "N"},
        "BANKNIFTY": {"root": "BANKNIFTY", "exch": "N"},
        "FINNIFTY":  {"root": "FINNIFTY",  "exch": "N"},
        "SENSEX":    {"root": "SENSEX",    "exch": "B"},
    }

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------------ #
    async def refresh_all(self, force: bool = False, symbols=None) -> Dict[str, Dict]:
        symbols = symbols or list(self.ROOT_MAP)
        if not force and self._has_fresh_mapping(symbols):
            logger.info("[Resolver] Mappings already refreshed today - reusing cache")
            return self.load_from_db()

        df = await self._fetch_master()
        if df is None or df.empty:
            logger.error("[Resolver] Scrip master unavailable - using last known mappings")
            return self.load_from_db()

        df = self._standardize(df)
        if df is None:
            return self.load_from_db()

        resolved: Dict[str, Dict] = {}
        for symbol in symbols:
            meta = self.ROOT_MAP.get(symbol)
            if not meta:
                continue
            match = self._pick_contract(df, meta)
            if match:
                resolved[symbol] = match
                self._save(symbol, meta["root"], match)
                logger.info("[Resolver] %s -> %s code=%s expiry=%s lot=%s",
                            symbol, match["ContractName"], match["ScripCode"],
                            match.get("Expiry"), match.get("LotSize"))
            else:
                logger.warning("[Resolver] No live future found for %s", symbol)

        merged = self.load_from_db()
        merged.update(resolved)
        return merged

    def is_expiry_day(self, symbol: str, check_date: datetime = None) -> bool:
        check = (check_date or now_naive()).date()
        with session_scope() as db:
            row = (db.query(ScripMapping)
                     .filter(ScripMapping.symbol == symbol, ScripMapping.is_current.is_(True))
                     .order_by(ScripMapping.updated_at.desc())
                     .first())
            if not row or not row.expiry_date:
                return False
            try:
                return datetime.strptime(row.expiry_date, "%Y-%m-%d").date() == check
            except ValueError:
                return False

    def any_expiry_today(self) -> bool:
        return any(self.is_expiry_day(s) for s in self.ROOT_MAP)

    # ------------------------------------------------------------------ #
    async def _fetch_master(self) -> Optional[pd.DataFrame]:
        def _fetch():
            try:
                data = self.client.get_scrips()
                if isinstance(data, list):
                    return pd.DataFrame(data)
                return data
            except Exception as exc:
                logger.error("[Resolver] get_scrips() failed: %s", exc)
                return None

        return await asyncio.to_thread(_fetch)

    @staticmethod
    def _standardize(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        df = df.copy()
        df.columns = [re.sub(r"[^a-z]", "", str(c).lower()) for c in df.columns]
        aliases = {
            "exch": "exch",
            "exchtype": "exchtype", "exchangetype": "exchtype",
            "scripcode": "scripcode", "code": "scripcode", "token": "scripcode",
            "name": "name", "symbol": "name", "scripname": "name", "fullname": "fullname",
            "symbolroot": "root", "root": "root", "underlyer": "root",
            "expiry": "expiry", "expirydate": "expiry",
            "scriptype": "scriptype",
            "lotsize": "lotsize", "boardlotquantity": "lotsize",
            "oi": "oi", "openinterest": "oi",
        }
        df = df.rename(columns={c: aliases[c] for c in df.columns if c in aliases})
        required = {"exch", "exchtype", "scripcode", "name"}
        if not required.issubset(df.columns):
            logger.warning("[Resolver] Scrip master is missing %s. Columns seen: %s",
                           required - set(df.columns), list(df.columns)[:25])
            return None
        return df

    def _pick_contract(self, df: pd.DataFrame, meta: Dict) -> Optional[Dict]:
        exch = str(meta["exch"]).upper()
        root = str(meta["root"]).upper()

        mask = (df["exch"].astype(str).str.upper() == exch) & \
               (df["exchtype"].astype(str).str.upper() == "D")
        deriv = df[mask]
        if deriv.empty:
            return None

        # Futures only: ScripType XX where available, otherwise exclude options
        # by name pattern and require a zero/absent strike.
        if "scriptype" in deriv.columns:
            futures = deriv[deriv["scriptype"].astype(str).str.upper().str.strip().isin(["XX", "FUT", "FUTIDX"])]
        else:
            names = deriv["name"].astype(str).str.upper()
            futures = deriv[~names.str.contains(r"(?:\d+\s*(?:CE|PE)\b)|(?:\s(?:CE|PE)$)", regex=True, na=False)]
        if futures.empty:
            return None

        root_col = "root" if "root" in futures.columns else "name"
        roots = futures[root_col].astype(str).str.upper().str.strip()
        futures = futures[(roots == root) | (roots.str.startswith(root + " ")) |
                          (futures["name"].astype(str).str.upper().str.startswith(root))]
        if futures.empty:
            return None

        futures = futures.copy()
        futures["parsed_expiry"] = futures.apply(self._expiry_of, axis=1)
        futures = futures[futures["parsed_expiry"].notna()]
        today = now_naive().date()
        futures = futures[futures["parsed_expiry"] >= today]
        if futures.empty:
            return None

        futures = futures.sort_values("parsed_expiry")
        best = futures.iloc[0]
        return {
            "Exch": str(best["exch"]).upper(),
            "ExchType": str(best["exchtype"]).upper(),
            "ScripCode": int(float(best["scripcode"])),
            "ContractName": str(best.get("name", "")),
            "Expiry": best["parsed_expiry"].strftime("%Y-%m-%d"),
            "LotSize": _safe_int(best.get("lotsize", 0)),
            "OI": _safe_int(best.get("oi", 0)),
        }

    # ------------------------------------------------------------------ #
    def _expiry_of(self, row) -> Optional[datetime.date]:
        if "expiry" in row.index:
            parsed = self._parse_date(row.get("expiry"))
            if parsed:
                return parsed
        return self._parse_expiry_from_name(str(row.get("name", "")))

    @staticmethod
    def _parse_expiry_from_name(name: str):
        if not name:
            return None
        match = re.search(r"(\d{1,2})[\s-]?([A-Z]{3})[\s-]?(\d{2,4})", name.upper())
        if not match:
            return None
        day, mon, year = match.groups()
        month = MONTHS.get(mon)
        if not month:
            return None
        year_i = int(year)
        if year_i < 100:
            year_i += 2000
        try:
            return datetime(year_i, month, int(day)).date()
        except ValueError:
            return None

    @staticmethod
    def _parse_date(value):
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in ("nan", "none", "nat"):
            return None
        ms = re.search(r"/Date\((\d+)", text)          # /Date(1712345678000)/
        if ms:
            try:
                return datetime.fromtimestamp(int(ms.group(1)) / 1000).date()
            except (ValueError, OSError):
                return None
        head = text.split()[0]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%b-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(head, fmt).date()
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _has_fresh_mapping(symbols) -> bool:
        with session_scope() as db:
            count = (db.query(ScripMapping)
                       .filter(ScripMapping.updated_at >= today_start(),
                               ScripMapping.is_current.is_(True),
                               ScripMapping.symbol.in_(symbols))
                       .count())
            return count >= len(symbols)

    @staticmethod
    def load_from_db() -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        with session_scope() as db:
            rows = (db.query(ScripMapping)
                      .filter(ScripMapping.is_current.is_(True))
                      .order_by(ScripMapping.updated_at.desc())
                      .all())
            for r in rows:
                out.setdefault(r.symbol, {
                    "Exch": r.exch, "ExchType": r.exch_type, "ScripCode": r.scrip_code,
                    "ContractName": r.contract_name, "Expiry": r.expiry_date,
                    "LotSize": r.lot_size or 0,
                })
        return out

    @staticmethod
    def _save(symbol: str, root: str, match: Dict) -> None:
        with session_scope() as db:
            (db.query(ScripMapping)
               .filter(ScripMapping.symbol == symbol)
               .update({"is_current": False}, synchronize_session=False))
            db.add(ScripMapping(
                symbol=symbol, root_name=root, scrip_code=match["ScripCode"],
                exch=match["Exch"], exch_type=match["ExchType"],
                contract_name=match["ContractName"], expiry_date=match.get("Expiry"),
                lot_size=match.get("LotSize", 0), oi=match.get("OI", 0),
                is_current=True, updated_at=now_naive(),
            ))


def _safe_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
