"""
Options overlay for the index signals.

The scanner detects the move on the futures. This module answers the separate
question of whether the *option* is a sane way to express it: which strike,
which expiry, is implied volatility rich or cheap, and is theta about to eat
the position alive.

Three things here are deliberately different from the usual scaffold version:

1. Implied volatility is solved from the live option price with Black-76, not
   hardcoded. A hardcoded IV rank makes the filter decorative.
2. IV rank is computed from stored history and returns None when there isn't
   enough of it. A fresh deployment has no history, and pretending otherwise
   would silently pass every trade.
3. A block is enforced by the signal engine, not just recorded on the dict.

Nothing here places an order. Option prices move on delta, theta and vega at
once, so the P&L the ledger tracks on futures levels will not match an option
position tick for tick.
"""
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

from clock import now_naive, today_start
from config import cfg
from database import IVSnapshot, session_scope

logger = logging.getLogger(__name__)

SQRT_2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0


# --------------------------------------------------------------------------- #
# Black-76: options on futures. Indian index options are European and we
# already price the futures leg, so working off F avoids needing a dividend
# assumption for the spot.
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1_d2(f: float, k: float, t: float, sigma: float):
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_t
    return d1, d1 - vol_t


def black76_price(f: float, k: float, t: float, sigma: float,
                  is_call: bool, r: float = None) -> float:
    """Undiscounted-forward option price. f = futures, t = years to expiry."""
    r = cfg.RISK_FREE_RATE if r is None else r
    if t <= 0 or sigma <= 0 or f <= 0 or k <= 0:
        intrinsic = max(0.0, (f - k) if is_call else (k - f))
        return intrinsic
    d1, d2 = _d1_d2(f, k, t, sigma)
    disc = math.exp(-r * t)
    if is_call:
        return disc * (f * _norm_cdf(d1) - k * _norm_cdf(d2))
    return disc * (k * _norm_cdf(-d2) - f * _norm_cdf(-d1))


def black76_greeks(f: float, k: float, t: float, sigma: float,
                   is_call: bool, r: float = None) -> Dict[str, float]:
    """Delta, gamma, vega (per 1 vol point), theta (per calendar day)."""
    r = cfg.RISK_FREE_RATE if r is None else r
    if t <= 0 or sigma <= 0 or f <= 0 or k <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}

    d1, d2 = _d1_d2(f, k, t, sigma)
    disc = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    price = black76_price(f, k, t, sigma, is_call, r)

    delta = disc * (_norm_cdf(d1) if is_call else -_norm_cdf(-d1))
    gamma = disc * _norm_pdf(d1) / (f * sigma * sqrt_t)
    vega = f * disc * _norm_pdf(d1) * sqrt_t / 100.0
    # dC/dT = -rC + e^-rT F n(d1) sigma / (2 sqrt(T)); theta is the negative.
    theta_year = r * price - disc * f * _norm_pdf(d1) * sigma / (2.0 * sqrt_t)

    return {"delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta_year / DAYS_PER_YEAR}


def implied_vol(price: float, f: float, k: float, t: float, is_call: bool,
                r: float = None) -> Optional[float]:
    """
    Invert Black-76 by bisection. Slower than Newton but it cannot diverge on
    deep-ITM or near-expiry quotes, which is exactly where this gets called.
    Returns None when the quote is outside no-arbitrage bounds.
    """
    r = cfg.RISK_FREE_RATE if r is None else r
    if price <= 0 or f <= 0 or k <= 0 or t <= 0:
        return None

    disc = math.exp(-r * t)
    intrinsic = disc * max(0.0, (f - k) if is_call else (k - f))
    upper_bound = disc * (f if is_call else k)
    if price <= intrinsic + 1e-8 or price >= upper_bound:
        return None

    lo, hi = 1e-4, 5.0
    if black76_price(f, k, t, hi, is_call, r) < price:
        return None

    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76_price(f, k, t, mid, is_call, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


def years_to_expiry(expiry: date, now: datetime = None) -> float:
    """Calendar time to the 15:30 IST close on expiry day, in years."""
    now = now or now_naive()
    expiry_dt = datetime.combine(expiry, time(15, 30))
    seconds = (expiry_dt - now).total_seconds()
    return max(seconds, 0.0) / (DAYS_PER_YEAR * 24 * 3600)


# --------------------------------------------------------------------------- #
@dataclass
class OptionPlan:
    symbol: str
    direction: str
    option_type: str = ""              # CE | PE
    strike: Optional[int] = None
    expiry: Optional[str] = None       # ISO date
    dte: Optional[int] = None
    scrip_code: Optional[int] = None
    ltp: Optional[float] = None
    iv: Optional[float] = None         # decimal, 0.14 == 14%
    iv_rank: Optional[float] = None    # 0-100, None when history is thin
    iv_samples: int = 0
    delta: Optional[float] = None
    theta_per_day: Optional[float] = None
    theta_pct_per_day: Optional[float] = None
    theta_cost_hold: Optional[float] = None    # premium lost over the expected hold
    expected_gain: Optional[float] = None      # delta x move to TP1
    theta_drag_pct: Optional[float] = None     # theta cost as % of expected gain
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    spread_pct: Optional[float] = None         # spread as % of premium
    spread_drag_pct: Optional[float] = None    # spread as % of expected gain
    open_interest: Optional[float] = None
    blocked: bool = False
    block_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if not self.strike:
            return ""
        root = self.symbol.split()[0]
        exp = f" {self.expiry}" if self.expiry else ""
        return f"{root} {self.strike} {self.option_type}{exp}"

    def to_dict(self) -> Dict:
        out = asdict(self)
        out["label"] = self.label
        return out


class OptionsLayer:
    def __init__(self, feed=None):
        self.feed = feed
        self.enabled = cfg.OPTIONS_ENABLED

    # ------------------------------------------------------------------ #
    async def build_plan(self, symbol: str, futures_price: float,
                         direction: str, target_move: float = None) -> OptionPlan:
        """Pick the contract, price its IV, rank it, and apply the gates."""
        plan = OptionPlan(symbol=symbol, direction=direction,
                          option_type="CE" if direction == "LONG" else "PE")
        if not self.enabled:
            plan.notes.append("Options layer disabled - futures levels only")
            return plan

        plan.strike = atm_strike(futures_price, cfg.strike_step(symbol))

        try:
            expiries = await self.feed.get_expiries(symbol)
        except Exception as exc:
            logger.warning("[Options] Expiry list unavailable for %s: %s", symbol, exc)
            plan.notes.append("Expiry calendar unavailable")
            return self._apply_block(plan, cfg.OPTIONS_BLOCK_ON_MISSING_DATA,
                                     "No expiry data")

        chosen = self._choose_expiry(expiries)
        if not chosen:
            return self._apply_block(plan, cfg.OPTIONS_BLOCK_ON_MISSING_DATA,
                                     "No tradable expiry found")

        plan.expiry = chosen["date"].isoformat()
        plan.dte = (chosen["date"] - now_naive().date()).days
        if chosen.get("rolled"):
            plan.notes.append("Rolled to the next expiry - today's contract is past the theta cutoff")

        # --- the theta gate -------------------------------------------------
        gate = self.theta_gate(chosen["date"])
        if gate:
            return self._apply_block(plan, True, gate)

        # --- price the contract and solve for IV -----------------------------
        try:
            quote = await self.feed.get_option_quote(symbol, chosen, plan.strike, plan.option_type)
        except Exception as exc:
            logger.warning("[Options] Chain lookup failed for %s: %s", symbol, exc)
            return self._apply_block(plan, cfg.OPTIONS_BLOCK_ON_MISSING_DATA,
                                     "Option chain unavailable")

        if not quote or not quote.get("ltp"):
            return self._apply_block(plan, cfg.OPTIONS_BLOCK_ON_MISSING_DATA,
                                     f"No quote for {plan.strike} {plan.option_type}")

        plan.ltp = round(float(quote["ltp"]), 2)
        plan.scrip_code = quote.get("scrip_code")
        plan.bid = quote.get("bid")
        plan.ask = quote.get("ask")
        plan.spread = quote.get("spread")
        plan.spread_pct = quote.get("spread_pct")
        plan.open_interest = quote.get("oi")

        t = years_to_expiry(chosen["date"])
        is_call = plan.option_type == "CE"
        iv = implied_vol(plan.ltp, futures_price, plan.strike, t, is_call)
        if iv is None:
            plan.notes.append("IV could not be solved from the quote")
            return self._apply_block(plan, cfg.OPTIONS_BLOCK_ON_MISSING_DATA,
                                     "IV unsolvable")

        plan.iv = round(iv, 4)
        greeks = black76_greeks(futures_price, plan.strike, t, iv, is_call)
        plan.delta = round(greeks["delta"], 4)
        plan.theta_per_day = round(greeks["theta"], 2)
        plan.theta_pct_per_day = round(abs(greeks["theta"]) / plan.ltp * 100, 2) if plan.ltp else None

        # What decay actually costs over a realistic hold, against what the
        # option should make if the underlying reaches TP1.
        hold_fraction = cfg.EXPECTED_HOLD_HOURS / 24.0
        plan.theta_cost_hold = round(abs(greeks["theta"]) * hold_fraction, 2)
        if target_move:
            plan.expected_gain = round(abs(greeks["delta"]) * abs(target_move), 2)
            if plan.expected_gain > 0:
                plan.theta_drag_pct = round(plan.theta_cost_hold / plan.expected_gain * 100, 1)
                # Crossing the spread is paid immediately and in full, so it is
                # measured the same way as theta: against the move to TP1.
                if plan.spread:
                    plan.spread_drag_pct = round(plan.spread / plan.expected_gain * 100, 1)

        # --- IV rank ----------------------------------------------------------
        self.record_iv(symbol, iv, futures_price, chosen["date"])
        rank, samples = self.iv_rank(symbol, iv)
        plan.iv_rank = rank
        plan.iv_samples = samples

        return self._apply_gates(plan)

    # ------------------------------------------------------------------ #
    def _choose_expiry(self, expiries: List[Dict]) -> Optional[Dict]:
        """
        Nearest expiry that hasn't passed. On expiry day past the theta cutoff,
        roll to the next one instead of buying a contract with hours to live.
        """
        today = now_naive().date()
        live = sorted([e for e in expiries if e.get("date") and e["date"] >= today],
                      key=lambda e: e["date"])
        if not live:
            return None

        nearest = live[0]
        if nearest["date"] == today and cfg.EXPIRY_DAY_ROLLOVER and self._past_theta_cutoff():
            if len(live) > 1:
                nxt = dict(live[1])
                nxt["rolled"] = True
                return nxt
        return nearest

    @staticmethod
    def _past_theta_cutoff(now: datetime = None) -> bool:
        now = now or now_naive()
        try:
            hh, mm = (int(x) for x in cfg.THETA_CUTOFF_TIME.split(":"))
        except ValueError:
            hh, mm = 13, 30
        return now.time() >= time(hh, mm)

    def theta_gate(self, expiry: date, now: datetime = None) -> Optional[str]:
        """
        Returns a block reason, or None if the contract is fine to buy.

        On expiry day an ATM option is pure extrinsic value with hours left;
        after the cutoff, decay outruns the move the scanner just detected.
        """
        now = now or now_naive()
        dte = (expiry - now.date()).days
        if dte < 0:
            return "Expiry has passed"
        if dte == 0 and self._past_theta_cutoff(now):
            return f"Expiry day past {cfg.THETA_CUTOFF_TIME} - theta cutoff"
        return None

    def _apply_gates(self, plan: OptionPlan) -> OptionPlan:
        """IV-rank band and the daily-theta ceiling."""
        if plan.iv_rank is None:
            plan.notes.append(
                f"IV rank needs {cfg.IV_RANK_MIN_SAMPLES} readings, have {plan.iv_samples} - "
                "gate not applied yet")
        else:
            if plan.iv_rank > cfg.MAX_IV_RANK:
                return self._apply_block(
                    plan, True,
                    f"IV rank {plan.iv_rank:.0f} above {cfg.MAX_IV_RANK} - premium is rich, "
                    "a crush would work against a buyer")
            if plan.iv_rank < cfg.MIN_IV_RANK:
                return self._apply_block(
                    plan, True,
                    f"IV rank {plan.iv_rank:.0f} below {cfg.MIN_IV_RANK} - the market isn't "
                    "pricing a move worth buying")

        if (cfg.MAX_THETA_PCT_PER_DAY > 0 and plan.theta_pct_per_day is not None
                and plan.theta_pct_per_day > cfg.MAX_THETA_PCT_PER_DAY):
            return self._apply_block(
                plan, True,
                f"Theta burns {plan.theta_pct_per_day:.1f}% of premium a day, "
                f"cap is {cfg.MAX_THETA_PCT_PER_DAY}%")

        if plan.theta_drag_pct is not None and plan.theta_drag_pct > cfg.MAX_THETA_DRAG_PCT:
            return self._apply_block(
                plan, True,
                f"Over a {cfg.EXPECTED_HOLD_HOURS:g}h hold theta costs "
                f"{plan.theta_cost_hold:.1f} against an expected {plan.expected_gain:.1f} "
                f"at TP1 ({plan.theta_drag_pct:.0f}% drag, cap {cfg.MAX_THETA_DRAG_PCT:.0f}%)")

        if plan.spread_drag_pct is not None and plan.spread_drag_pct > cfg.MAX_SPREAD_DRAG_PCT:
            return self._apply_block(
                plan, True,
                f"Bid-ask spread of {plan.spread:.2f} costs {plan.spread_drag_pct:.0f}% of the "
                f"expected {plan.expected_gain:.1f} move to TP1 (cap {cfg.MAX_SPREAD_DRAG_PCT:.0f}%)")

        if plan.open_interest is not None and 0 < plan.open_interest < cfg.MIN_OPTION_OI:
            return self._apply_block(
                plan, True,
                f"Strike has {plan.open_interest:,.0f} OI, below {cfg.MIN_OPTION_OI:,.0f} - "
                "a tight quote on an empty book will not fill")

        if plan.spread is None:
            plan.notes.append("Feed publishes no depth - spread gate not applied")

        if plan.dte is not None and plan.dte <= 1:
            plan.notes.append("One day or less to expiry - size down")
        return plan

    @staticmethod
    def _apply_block(plan: OptionPlan, block: bool, reason: str) -> OptionPlan:
        if block:
            plan.blocked = True
            plan.block_reason = reason
        else:
            plan.notes.append(reason)
        return plan

    # ------------------------------------------------------------------ #
    @staticmethod
    def record_iv(symbol: str, iv: float, futures_price: float, expiry: date) -> None:
        """One reading per symbol per bar-ish. Cheap, and it's what makes rank real."""
        try:
            with session_scope() as db:
                db.add(IVSnapshot(symbol=symbol, atm_iv=float(iv),
                                  futures_price=float(futures_price),
                                  expiry=expiry.isoformat(), timestamp=now_naive()))
        except Exception as exc:
            logger.warning("[Options] Could not store IV for %s: %s", symbol, exc)

    @staticmethod
    def iv_rank(symbol: str, current_iv: float):
        """
        Where today's IV sits in its own recent range, 0-100.

        Textbook IV rank uses a year of daily readings. A container that
        deployed last week has days, not a year, so this returns
        (None, sample_count) until there's enough history rather than
        producing a confident number from four data points.
        """
        cutoff = now_naive() - timedelta(days=cfg.IV_RANK_LOOKBACK_DAYS)
        try:
            with session_scope() as db:
                rows = (db.query(IVSnapshot)
                          .filter(IVSnapshot.symbol == symbol,
                                  IVSnapshot.timestamp >= cutoff,
                                  IVSnapshot.atm_iv.isnot(None))
                          .all())
                values = [float(r.atm_iv) for r in rows if r.atm_iv]
                days = len({r.timestamp.date() for r in rows if r.timestamp})
        except Exception as exc:
            logger.warning("[Options] IV history unavailable for %s: %s", symbol, exc)
            return None, 0

        if len(values) < cfg.IV_RANK_MIN_SAMPLES or days < cfg.IV_RANK_MIN_DAYS:
            return None, len(values)

        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            return 50.0, len(values)
        rank = (current_iv - lo) / (hi - lo) * 100.0
        return round(max(0.0, min(100.0, rank)), 1), len(values)

    # ------------------------------------------------------------------ #
    async def record_atm_iv_for_universe(self) -> int:
        """
        Scheduled job. Builds IV history even on days with no signal, which is
        the only way the rank filter is ever worth anything.
        """
        if not self.enabled or not self.feed:
            return 0
        stored = 0
        for symbol in cfg.INDICES:
            try:
                quote = await self.feed.get_live_quote(symbol, use_futures=True)
                futures_price = float(quote["ltp"])
                expiries = await self.feed.get_expiries(symbol)
                chosen = self._choose_expiry(expiries)
                if not chosen or futures_price <= 0:
                    continue
                strike = atm_strike(futures_price, cfg.strike_step(symbol))
                opt = await self.feed.get_option_quote(symbol, chosen, strike, "CE")
                if not opt or not opt.get("ltp"):
                    continue
                t = years_to_expiry(chosen["date"])
                iv = implied_vol(float(opt["ltp"]), futures_price, strike, t, True)
                if iv:
                    self.record_iv(symbol, iv, futures_price, chosen["date"])
                    stored += 1
            except Exception as exc:
                logger.warning("[Options] IV sampling failed for %s: %s", symbol, exc)
        if stored:
            logger.info("[Options] Stored %d ATM IV reading(s)", stored)
        return stored


# Imported late to keep the indicator module free of option concerns.
from indicators import atm_strike  # noqa: E402
