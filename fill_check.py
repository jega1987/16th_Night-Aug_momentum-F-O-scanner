"""
Live-fill check for paper signals (22 Nov 2026).

Before trading real money the user wants to know how far a real order would
have filled from the paper price. For every option leg the paper engine books
(entry BUY, scale-outs and the final SELL), this re-quotes the same contract
from the Kite market feed a few seconds apart and records:

  * best bid / ask / last price at each check, and
  * the price a market order for the trade's quantity would have got by
    walking the 5-level order book (None when the book is too thin).

Slippage is "worse for us" positive: paying more on a BUY, getting less on a
SELL, in rupees per unit and in % of the paper price. Nothing is ordered -
this only reads quotes. One REST quote per check, a handful per trade, well
inside Kite's 1 request/second quote limit.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

from clock import now_naive
from config import cfg
from database import FillCheck, session_scope

logger = logging.getLogger(__name__)

# Seconds after the paper fill at which the book is sampled. 0 = an order sent
# at the same moment as the paper fill; later ones show the cost of waiting
# (a manual order placed from the Telegram alert, say).
OFFSETS = (0, 5, 15, 30, 60)


def depth_fill(levels: List[Dict], qty: int) -> Optional[float]:
    """Average price a market order for `qty` gets walking these book levels."""
    if not qty or qty <= 0:
        return None
    left, cost = qty, 0.0
    for lv in levels or []:
        price, avail = float(lv.get("price") or 0), int(lv.get("quantity") or 0)
        if price <= 0 or avail <= 0:
            continue
        take = min(left, avail)
        cost += take * price
        left -= take
        if left == 0:
            return round(cost / qty, 2)
    return None                       # not enough quantity in the top 5 levels


def slippage(side: str, paper: float, real: Optional[float]) -> Optional[float]:
    """Rupees per unit, positive = worse than the paper price."""
    if real is None or not paper:
        return None
    return round((real - paper) if side == "BUY" else (paper - real), 2)


class FillChecker:
    def __init__(self, feed):
        self.feed = feed
        self._tasks: set = set()

    def enabled(self) -> bool:
        return (getattr(cfg, "FILL_CHECK_ENABLED", True)
                and getattr(self.feed, "kite", None) is not None)

    def schedule(self, signal_id: int, leg: str, side: str, token: Optional[int],
                 qty: int, paper_price: Optional[float], paper_at: datetime = None) -> None:
        """Start the checks in the background; never raises."""
        try:
            if not (self.enabled() and token and paper_price and qty):
                return
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._run(int(signal_id), leg, side, int(token), int(qty),
                                              float(paper_price), paper_at or now_naive()))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as exc:
            logger.debug("[Fill] could not schedule %s #%s: %s", leg, signal_id, exc)

    async def _quote(self, token: int) -> Optional[Dict]:
        kite = self.feed.kite
        call = getattr(self.feed, "_call", None)
        fn = lambda: kite.quote([token])
        data = await (call(fn, f"fill check {token}", attempts=1) if call
                      else asyncio.to_thread(fn))
        data = data or {}
        return data.get(str(token)) or data.get(token)

    async def _run(self, signal_id: int, leg: str, side: str, token: int, qty: int,
                   paper: float, paper_at: datetime) -> None:
        samples, waited = [], 0
        for off in OFFSETS:
            await asyncio.sleep(max(0, off - waited))
            waited = off
            try:
                row = await self._quote(token)
            except Exception as exc:
                samples.append({"sec": off, "error": str(exc)[:80]})
                continue
            if not row:
                samples.append({"sec": off, "error": "no quote"})
                continue
            depth = row.get("depth") or {}
            bids, asks = depth.get("buy") or [], depth.get("sell") or []
            bid = float(bids[0]["price"]) if bids and bids[0].get("price") else None
            ask = float(asks[0]["price"]) if asks and asks[0].get("price") else None
            book_fill = depth_fill(asks if side == "BUY" else bids, qty)
            samples.append({
                "sec": off, "at": now_naive().strftime("%H:%M:%S"),
                "ltp": float(row.get("last_price") or 0) or None,
                "bid": bid, "ask": ask, "fill": book_fill,
                "slip": slippage(side, paper, book_fill),
            })
        self._save(signal_id, leg, side, token, qty, paper, paper_at, samples)

    @staticmethod
    def _save(signal_id, leg, side, token, qty, paper, paper_at, samples) -> None:
        def at(sec):
            for s in samples:
                if s.get("sec") == sec:
                    return s.get("slip")
            return None
        first = samples[0] if samples else {}
        spread = (first["ask"] - first["bid"]) if first.get("ask") and first.get("bid") else None
        s0 = at(0)
        try:
            with session_scope() as db:
                db.add(FillCheck(
                    signal_id=signal_id, leg=leg, side=side, token=token, qty=qty,
                    paper_price=round(paper, 2), paper_at=paper_at, samples=samples,
                    spread_0s=round(spread, 2) if spread is not None else None,
                    slip_0s=s0, slip_15s=at(15), slip_60s=at(60),
                    slip_pct_0s=round(s0 / paper * 100, 2) if s0 is not None and paper else None,
                    thin_book=any(s.get("fill") is None and not s.get("error") for s in samples),
                ))
            logger.info("[Fill] #%s %s %s x%d paper %.2f | slip 0s %s, 15s %s, 60s %s",
                        signal_id, leg, side, qty, paper, s0, at(15), at(60))
        except Exception as exc:
            logger.warning("[Fill] save failed for #%s %s: %s", signal_id, leg, exc)


def summary_for(signal_ids: List[int]) -> Dict[int, Dict]:
    """{signal_id: {"ENTRY": {...}, "T1": {...}, "EXIT": {...}, "total_rs": ...}} for the dashboard."""
    out: Dict[int, Dict] = {}
    if not signal_ids:
        return out
    with session_scope() as db:
        rows = db.query(FillCheck).filter(FillCheck.signal_id.in_(list(signal_ids))).all()
        for r in rows:
            d = out.setdefault(r.signal_id, {"legs": {}, "total_rs": 0.0})
            d["legs"][r.leg] = {
                "side": r.side, "qty": r.qty, "paper": r.paper_price,
                "slip_0s": r.slip_0s, "slip_15s": r.slip_15s, "slip_60s": r.slip_60s,
                "slip_pct_0s": r.slip_pct_0s, "spread_0s": r.spread_0s,
                "thin": bool(r.thin_book), "samples": r.samples,
            }
            if r.slip_0s is not None:
                d["total_rs"] = round(d["total_rs"] + r.slip_0s * (r.qty or 0), 2)
    return out
