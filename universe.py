"""
Daily equity universe selection.

The F&O list changes with exchange circulars, and liquidity within it changes
weekly, so the tradable set is rebuilt each morning rather than hardcoded.

Selection rules, in order:
  1. Start from the derivatives segment of the scrip master (stock futures).
  2. Drop anything trading below MIN_STOCK_PRICE. A 2% move on a Rs 80 stock is
     noise, and its option strikes are too coarse relative to the premium.
  3. Drop anything below MIN_TURNOVER_CR of daily traded value - a tight
     underlying spread means nothing if the option book is empty.
  4. Rank the survivors on a blend of turnover and momentum, keep the top N.

Turnover is used rather than raw share volume: 10 lakh shares of a Rs 150 stock
and 10 lakh of a Rs 3,000 stock are not comparable liquidity.

Momentum degrades honestly. With enough stored daily closes it uses an N-day
rate of change; without them it falls back to today's percentage change and
says so, rather than quietly scoring zero.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from clock import now_naive, today_start
from config import cfg
from database import Candle, UniverseMember, session_scope

logger = logging.getLogger(__name__)


class UniverseBuilder:
    def __init__(self, feed):
        self.feed = feed
        self.last_built: Optional[str] = None
        self.last_reason: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    async def build(self, force: bool = False) -> List[str]:
        """Returns the ranked list of tradable stock symbols for today."""
        if not cfg.EQUITY_ENABLED:
            return []
        if not force and self._built_today():
            cached = self.load()
            logger.info("[Universe] Reusing today's list (%d symbols)", len(cached))
            return cached

        candidates = await self.feed.get_fno_stocks()
        if not candidates:
            logger.warning("[Universe] No F&O stocks returned - keeping yesterday's list")
            return self.load()
        logger.info("[Universe] %d F&O stocks in the scrip master", len(candidates))

        quotes = await self.feed.get_bulk_quotes(candidates)
        if not quotes:
            logger.warning("[Universe] No quotes returned - keeping yesterday's list")
            return self.load()

        rows, dropped = [], {"no_quote": 0, "below_price": 0, "below_turnover": 0}
        for c in candidates:
            q = quotes.get(c["symbol"])
            if not q or not q.get("ltp"):
                dropped["no_quote"] += 1
                continue
            ltp = float(q["ltp"])
            if ltp < cfg.MIN_STOCK_PRICE:
                dropped["below_price"] += 1
                continue

            turnover_cr = (ltp * float(q.get("volume") or 0)) / 1e7
            if turnover_cr < cfg.MIN_TURNOVER_CR:
                dropped["below_turnover"] += 1
                continue

            momentum, source = self._momentum(c["symbol"], ltp, q)
            rows.append({
                "symbol": c["symbol"], "root": c["root"], "scrip_code": c["scrip_code"],
                "exch": c["exch"], "exch_type": c["exch_type"], "lot_size": c.get("lot_size", 0),
                "expiry": c.get("expiry"), "ltp": ltp, "turnover_cr": round(turnover_cr, 2),
                "momentum": momentum, "momentum_source": source,
            })

        self.last_reason = dropped
        if not rows:
            logger.warning("[Universe] Everything filtered out (%s) - keeping yesterday's list", dropped)
            return self.load()

        ranked = self._rank(rows)[:cfg.UNIVERSE_SIZE]
        self._save(ranked)
        self.last_built = now_naive().date().isoformat()

        logger.info("[Universe] %d selected from %d candidates "
                    "(dropped: %d no quote, %d under Rs %.0f, %d under Rs %.0f cr turnover)",
                    len(ranked), len(candidates), dropped["no_quote"], dropped["below_price"],
                    cfg.MIN_STOCK_PRICE, dropped["below_turnover"], cfg.MIN_TURNOVER_CR)
        if ranked:
            top = ", ".join(f"{r['symbol']}({r['turnover_cr']:.0f}cr)" for r in ranked[:5])
            logger.info("[Universe] Top 5: %s", top)
        return [r["symbol"] for r in ranked]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _rank(rows: List[Dict]) -> List[Dict]:
        """
        Percentile-rank each measure separately, then blend.

        Ranking beats raw values here because turnover is heavily skewed - a
        couple of index heavyweights would otherwise dominate any weighted sum
        of the raw numbers and momentum would never matter.
        """
        n = len(rows)
        if n == 1:
            rows[0]["rank_score"] = 1.0
            return rows

        by_turnover = sorted(range(n), key=lambda i: rows[i]["turnover_cr"])
        by_momentum = sorted(range(n), key=lambda i: abs(rows[i]["momentum"]))
        pct_t, pct_m = [0.0] * n, [0.0] * n
        for pos, i in enumerate(by_turnover):
            pct_t[i] = pos / (n - 1)
        for pos, i in enumerate(by_momentum):
            pct_m[i] = pos / (n - 1)

        w_t, w_m = cfg.RANK_W_TURNOVER, cfg.RANK_W_MOMENTUM
        total = (w_t + w_m) or 1.0
        for i, row in enumerate(rows):
            row["pct_turnover"] = round(pct_t[i], 3)
            row["pct_momentum"] = round(pct_m[i], 3)
            row["rank_score"] = round((pct_t[i] * w_t + pct_m[i] * w_m) / total, 4)
        return sorted(rows, key=lambda r: -r["rank_score"])

    @staticmethod
    def _momentum(symbol: str, ltp: float, quote: Dict):
        """
        N-day rate of change when daily history exists, otherwise today's move.
        Returns (value_pct, source) so the caller can see which it got.
        """
        with session_scope() as db:
            closes = (db.query(Candle.close)
                        .filter(Candle.symbol == symbol, Candle.timeframe == "1d")
                        .order_by(Candle.timestamp.desc())
                        .limit(cfg.MOMENTUM_DAYS + 1)
                        .all())
        if len(closes) >= cfg.MOMENTUM_DAYS + 1:
            past = float(closes[-1][0])
            if past > 0:
                return round((ltp - past) / past * 100, 2), f"{cfg.MOMENTUM_DAYS}d"

        prev = float(quote.get("prev_close") or 0)
        if prev > 0:
            return round((ltp - prev) / prev * 100, 2), "1d"
        return 0.0, "none"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _built_today() -> bool:
        with session_scope() as db:
            return db.query(UniverseMember).filter(
                UniverseMember.selected_on >= today_start()).count() > 0

    @staticmethod
    def _save(ranked: List[Dict]) -> None:
        stamp = now_naive()
        with session_scope() as db:
            db.query(UniverseMember).filter(
                UniverseMember.selected_on >= today_start()).delete(synchronize_session=False)
            for i, r in enumerate(ranked):
                db.add(UniverseMember(
                    selected_on=stamp, rank=i + 1, symbol=r["symbol"], root=r["root"],
                    scrip_code=r["scrip_code"], exch=r["exch"], exch_type=r["exch_type"],
                    lot_size=r.get("lot_size", 0), expiry=r.get("expiry"),
                    ltp=r["ltp"], turnover_cr=r["turnover_cr"],
                    momentum_pct=r["momentum"], momentum_source=r["momentum_source"],
                    rank_score=r["rank_score"]))

    @staticmethod
    def load(limit: int = None) -> List[str]:
        """Most recent stored selection, newest run wins."""
        with session_scope() as db:
            latest = (db.query(UniverseMember.selected_on)
                        .order_by(UniverseMember.selected_on.desc()).first())
            if not latest:
                return []
            rows = (db.query(UniverseMember)
                      .filter(UniverseMember.selected_on == latest[0])
                      .order_by(UniverseMember.rank)
                      .limit(limit or cfg.UNIVERSE_SIZE).all())
            return [r.symbol for r in rows]

    @staticmethod
    def detail(limit: int = 50) -> List[Dict]:
        with session_scope() as db:
            latest = (db.query(UniverseMember.selected_on)
                        .order_by(UniverseMember.selected_on.desc()).first())
            if not latest:
                return []
            rows = (db.query(UniverseMember)
                      .filter(UniverseMember.selected_on == latest[0])
                      .order_by(UniverseMember.rank).limit(limit).all())
            return [{
                "rank": r.rank, "symbol": r.symbol, "ltp": r.ltp,
                "turnover_cr": r.turnover_cr, "momentum_pct": r.momentum_pct,
                "momentum_source": r.momentum_source, "lot_size": r.lot_size,
                "score": r.rank_score,
                "selected_on": r.selected_on.strftime("%d-%m %H:%M") if r.selected_on else None,
            } for r in rows]

    @staticmethod
    def scrips_for_stream(symbols: List[str]) -> List[Dict]:
        """Subscription payload entries for the websocket."""
        with session_scope() as db:
            rows = (db.query(UniverseMember)
                      .filter(UniverseMember.symbol.in_(symbols))
                      .order_by(UniverseMember.selected_on.desc()).all())
        seen, out = set(), []
        for r in rows:
            if r.symbol in seen:
                continue
            seen.add(r.symbol)
            out.append({"Exch": r.exch, "ExchType": r.exch_type, "ScripCode": r.scrip_code})
        return out
