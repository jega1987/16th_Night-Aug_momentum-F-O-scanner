"""
Position sizing, target/stop placement, and trade management.

This is a paper-trading tracker: it records what a trade would have done. It
places no orders. Nothing here is investment advice - size and risk settings
are yours to verify before you act on any of it.

Lifecycle
    OPEN     -> full size live
    RUNNING  -> TP1 (and maybe TP2) banked, remainder trailing
    TP3 | SL | TRAIL | SQUAREOFF -> closed
"""
import logging
import math
from datetime import date, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from clock import MarketClock, _parse_time, now_naive, today_start
from config import cfg
from database import Signal, session_scope
from indicators import atm_strike, supertrend

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, notifier=None, options=None):
        self.notifier = notifier
        self.options = options

    # ------------------------------------------------------------------ #
    def calculate_levels(self, setup: Dict) -> Optional[Dict]:
        """
        Stop at a market level, targets in multiples of that stop, quantity
        from the capital the trade ties up. The stop never looks at the
        account; the account only decides how many lots ride behind it.
        """
        entry = float(setup["entry"])
        atr_val = float(setup["atr"])
        symbol = setup["symbol"]
        sign = 1 if setup["direction"] == "LONG" else -1

        sl_dist, sl_basis = self._stop_distance(setup, entry, atr_val, symbol)
        max_sl_pct = cfg.max_sl_pct(symbol)
        if sl_dist <= 0:
            return None
        if (sl_dist / entry) * 100 > max_sl_pct:
            logger.info("[Engine] %s skipped - stop is %.2f%% away (%s), cap is %.2f%%",
                        symbol, (sl_dist / entry) * 100, sl_basis, max_sl_pct)
            return None

        lot = int(setup.get("lot_size") or 0) or cfg.lot_size(symbol)
        sized = self._size(symbol, entry, sl_dist, lot)
        if not sized:
            return None
        lots, qty, risk_pct, size_basis = sized

        if cfg.TARGET_MODE == "atr":
            t1 = atr_val * cfg.ATR_TP1_MULT
            t2 = atr_val * cfg.ATR_TP2_MULT
            t3 = atr_val * cfg.ATR_TP3_MULT
        else:
            r1, r2, r3 = cfg.TARGET_R_MULTS
            t1, t2, t3 = sl_dist * r1, sl_dist * r2, sl_dist * r3

        strike = atm_strike(entry, cfg.strike_step(symbol))
        opt_type = "CE" if setup["direction"] == "LONG" else "PE"

        return {
            "entry": round(entry, 2),
            "sl": round(entry - sign * sl_dist, 2),
            "tp1": round(entry + sign * t1, 2),
            "tp2": round(entry + sign * t2, 2),
            "tp3": round(entry + sign * t3, 2),
            "qty": qty,
            "lots": lots,
            "risk_pct": round(risk_pct, 3),
            "sl_basis": sl_basis,
            "size_basis": size_basis,
            "atm_strike": strike,
            "option_hint": f"{symbol.split()[0]} {strike} {opt_type}",
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _stop_distance(setup: Dict, entry: float, atr_val: float, symbol: str):
        """
        The widest of four market-derived distances. Returns (distance, basis)
        where basis names the rule that set it, so a post-mortem can see which
        floor actually places the stops.

        A squeeze compresses the 5-minute ATR by construction, so an ATR-only
        stop lands inside the noise the release is about to create. Half the
        coil, the higher-timeframe ATR and a % of price are the other three
        candidates; whichever is widest wins.
        """
        candidates = {"atr": atr_val * cfg.sl_mult(symbol)}

        hi, lo = setup.get("squeeze_high"), setup.get("squeeze_low")
        if hi is not None and lo is not None and float(hi) > float(lo):
            candidates["range"] = (float(hi) - float(lo)) * cfg.SL_RANGE_FRACTION

        atr_htf = setup.get("atr_htf")
        if atr_htf:
            candidates["htf_atr"] = float(atr_htf) * cfg.SL_HTF_ATR_MULT

        # Positional: the stop has to survive an overnight gap, so it is also
        # at least POS_SL_ATR_MULT x the hourly ATR.
        atr_pos = setup.get("atr_pos")
        if cfg.is_positional and atr_pos:
            candidates["pos_atr"] = float(atr_pos) * cfg.POS_SL_ATR_MULT

        candidates["min_pct"] = entry * cfg.min_sl_pct(symbol) / 100

        basis = max(candidates, key=candidates.get)
        dist = candidates[basis]
        detail = ", ".join(f"{k} {v:.2f}" for k, v in candidates.items())
        return dist, f"{basis} ({detail})"

    @staticmethod
    def _size(symbol: str, entry: float, sl_dist: float, lot: int):
        """
        Lots for the trade. Returns (lots, qty, risk_pct, basis) or None.

        notional: lots = CAPITAL_PER_TRADE / (one lot's margin). The stop is
                  already fixed by the market, so the rupee loss on a stop-out
                  is whatever that distance times this quantity comes to; it
                  is reported as risk_pct, not enforced.
        risk:     lots chosen so a stop-out costs RISK_PER_TRADE_PCT of the
                  account, capped at MAX_RISK_PER_TRADE_PCT.
        """
        if lot <= 0:
            logger.info("[Engine] %s skipped - lot size unknown", symbol)
            return None

        if cfg.SIZING_MODE == "risk":
            risk_budget = cfg.ACCOUNT_BALANCE * (cfg.RISK_PER_TRADE_PCT / 100)
            lots = math.floor((risk_budget / sl_dist) / lot)
            if lots < 1:
                logger.info("[Engine] %s skipped - one lot risks more than %.2f%% of the account",
                            symbol, cfg.RISK_PER_TRADE_PCT)
                return None
            qty = lots * lot
            risk_pct = (qty * sl_dist / cfg.ACCOUNT_BALANCE) * 100
            if risk_pct > cfg.MAX_RISK_PER_TRADE_PCT:
                lots = math.floor((cfg.ACCOUNT_BALANCE * cfg.MAX_RISK_PER_TRADE_PCT / 100)
                                  / (sl_dist * lot))
                if lots < 1:
                    return None
                qty = lots * lot
                risk_pct = (qty * sl_dist / cfg.ACCOUNT_BALANCE) * 100
            basis = f"risk {cfg.RISK_PER_TRADE_PCT:.1f}% of {cfg.ACCOUNT_BALANCE:,.0f}"
        else:
            margin_per_lot = entry * lot * cfg.margin_pct(symbol) / 100
            if margin_per_lot <= 0:
                return None
            lots = math.floor(cfg.CAPITAL_PER_TRADE / margin_per_lot)
            if cfg.MAX_LOTS_PER_TRADE > 0:
                lots = min(lots, cfg.MAX_LOTS_PER_TRADE)
            if lots < 1:
                logger.info("[Engine] %s skipped - one lot needs ~Rs %.0f margin, "
                            "CAPITAL_PER_TRADE is Rs %.0f",
                            symbol, margin_per_lot, cfg.CAPITAL_PER_TRADE)
                return None
            qty = lots * lot
            risk_pct = (qty * sl_dist / cfg.ACCOUNT_BALANCE) * 100
            basis = (f"notional {cfg.CAPITAL_PER_TRADE:,.0f} / margin {margin_per_lot:,.0f} per lot "
                     f"@ {cfg.margin_pct(symbol):.0f}%")

        return lots, qty, risk_pct, basis

    # ------------------------------------------------------------------ #
    async def create_signal(self, setup: Dict) -> Optional[Dict]:
        levels = self.calculate_levels(setup)
        if not levels:
            return None

        # --- options overlay -------------------------------------------------
        plan = None
        if self.options and cfg.OPTIONS_ENABLED:
            try:
                plan = await self.options.build_plan(
                    setup["symbol"], levels["entry"], setup["direction"],
                    target_move=abs(levels["tp1"] - levels["entry"]),
                )
            except Exception as exc:
                logger.warning("[Engine] Options overlay failed for %s: %s", setup["symbol"], exc)

        if plan and plan.blocked:
            if cfg.OPTIONS_BLOCK_SIGNAL:
                logger.info("[Engine] %s %s dropped - %s",
                            setup["symbol"], setup["direction"], plan.block_reason)
                return None
            logger.info("[Engine] %s %s kept as a futures-level signal - %s",
                        setup["symbol"], setup["direction"], plan.block_reason)

        scores = setup.get("scores", {})
        opt = self._option_levels(plan, levels)
        with session_scope() as db:
            row = Signal(
                timestamp=setup.get("timestamp") or now_naive(),
                symbol=setup["symbol"], timeframe=setup["timeframe"],
                direction=setup["direction"],
                asset_class=("INDEX" if cfg.is_index(setup["symbol"]) else "EQUITY"),
                entry=levels["entry"], sl=levels["sl"], trail_sl=levels["sl"],
                tp1=levels["tp1"], tp2=levels["tp2"], tp3=levels["tp3"],
                qty=levels["qty"], lots=levels["lots"], qty_open=levels["qty"],
                atr14=setup["atr"],
                atm_strike=(plan.strike if plan and plan.strike else levels["atm_strike"]),
                option_hint=(plan.label if plan and plan.label else levels["option_hint"]),
                option_type=(plan.option_type if plan else None),
                option_expiry=(plan.expiry if plan else None),
                option_dte=(plan.dte if plan else None),
                option_ltp=(plan.ltp if plan else None),
                option_iv=(plan.iv if plan else None),
                option_iv_rank=(plan.iv_rank if plan else None),
                option_delta=(plan.delta if plan else None),
                option_theta_pct=(plan.theta_pct_per_day if plan else None),
                option_blocked=bool(plan.blocked) if plan else False,
                option_block_reason=(plan.block_reason if plan else None),
                opt_entry=opt.get("opt_entry"), opt_sl=opt.get("opt_sl"),
                opt_tp1=opt.get("opt_tp1"), opt_tp2=opt.get("opt_tp2"), opt_tp3=opt.get("opt_tp3"),
                opt_ltp_now=opt.get("opt_entry"), opt_realized=0.0,
                opt_pnl=(0.0 if opt.get("opt_entry") else None),
                opt_qty_open=(levels["qty"] if opt.get("opt_entry") else None),
                score_direction=scores.get("direction"),
                score_squeeze=scores.get("squeeze"),
                score_sweep=scores.get("sweep"),
                score_structure=scores.get("structure"),
                score_volume=scores.get("volume"),
                score_rsi=scores.get("rsi"),
                score_oi=scores.get("oi"),
                score_adx=scores.get("adx"),
                score_htf=scores.get("htf"),
                composite_score=scores.get("composite"),
                factor_breakdown={**scores, **setup.get("meta", {}),
                                  "sl_basis": levels.get("sl_basis"),
                                  "size_basis": levels.get("size_basis"),
                                  "risk_pct": levels.get("risk_pct"),
                                  "bar_close": setup.get("bar_close"),
                                  "atr_htf": setup.get("atr_htf")},
                status="OPEN", realized_pnl=0.0, pnl=0.0,
                mfe=levels["entry"],
                triggered_by=setup.get("triggered_by", "scanner"),
            )
            db.add(row)
            db.flush()
            payload = _as_dict(row)

        if plan:
            payload["option_plan"] = plan.to_dict()

        logger.info("[Engine] %s %s @ %.2f | SL %.2f (%s) | TP %.2f/%.2f/%.2f | %d qty = %d lots "
                    "(%s) | risk %.2f%% | %s%s",
                    payload["symbol"], payload["direction"], payload["entry"], payload["sl"],
                    levels.get("sl_basis", "").split(" (")[0],
                    payload["tp1"], payload["tp2"], payload["tp3"], payload["qty"], payload["lots"],
                    levels.get("size_basis", ""), levels.get("risk_pct", 0.0),
                    payload["option_hint"],
                    f" | OPTION BLOCKED: {plan.block_reason}" if plan and plan.blocked else "")

        if self.notifier:
            await self.notifier.send_signal(payload)
        return payload

    # ------------------------------------------------------------------ #
    @staticmethod
    def _option_levels(plan, levels: Dict) -> Dict:
        """
        Option premium at the entry, stop and targets. The entry is the live
        quote; each level re-prices the same contract with Black-76 at that
        stock/futures price, keeping today's IV and time to expiry, and shifts
        it by the gap between model and market at the entry. An estimate:
        IV changes and time decay move the real premium.
        """
        if (not plan or not plan.ltp or not plan.iv or not plan.expiry
                or not plan.strike or plan.option_type not in ("CE", "PE")):
            return {}
        try:
            from options_layer import black76_price, years_to_expiry
            t = years_to_expiry(date.fromisoformat(str(plan.expiry)[:10]))
            if t <= 0:
                return {}
            is_call = plan.option_type == "CE"
            k, iv = float(plan.strike), float(plan.iv)
            base = black76_price(float(levels["entry"]), k, t, iv, is_call)

            def at(level):
                return round(max(0.05, plan.ltp + black76_price(float(level), k, t, iv, is_call) - base), 2)

            return {"opt_entry": round(float(plan.ltp), 2), "opt_sl": at(levels["sl"]),
                    "opt_tp1": at(levels["tp1"]), "opt_tp2": at(levels["tp2"]),
                    "opt_tp3": at(levels["tp3"])}
        except Exception as exc:
            logger.warning("[Engine] Option levels unavailable for %s: %s", plan.symbol, exc)
            return {}

    @staticmethod
    async def _quote_option(feed, s: Dict) -> Optional[float]:
        """Current premium (bid/ask mid where quoted) of the signal's contract."""
        if not (s.get("opt_entry") and s.get("option_expiry") and s.get("atm_strike")
                and s.get("option_type")):
            return None
        try:
            exp = date.fromisoformat(str(s["option_expiry"])[:10])
            q = await feed.get_option_quote(s["symbol"], {"date": exp}, int(s["atm_strike"]),
                                            s["option_type"])
            price = float((q or {}).get("ltp") or 0)
            return price if price > 0 else None
        except Exception as exc:
            logger.debug("[Engine] Option re-quote failed for %s: %s", s.get("symbol"), exc)
            return None

    @staticmethod
    def _update_option(s: Dict, price: Optional[float], qty_before: int) -> bool:
        """
        Follow the stock-level scale-outs with the option: quantity closed in
        this cycle is booked at the current premium; the rest is marked to it.
        Buying a CE (LONG) or PE (SHORT), so P&L is always premium change x qty.
        """
        entry = s.get("opt_entry")
        if not entry:
            return False
        if price is None:
            price = s.get("opt_ltp_now") or entry       # no fresh quote: last known
        qty_after = s.get("qty_open") or 0
        closed_qty = max(0, (qty_before or 0) - qty_after)
        if closed_qty:
            s["opt_realized"] = round((s.get("opt_realized") or 0) + (price - entry) * closed_qty, 2)
        s["opt_qty_open"] = qty_after
        s["opt_ltp_now"] = round(price, 2)
        s["opt_updated_at"] = now_naive()
        if s.get("status") in ("TP3", "SL", "TRAIL", "SQUAREOFF"):
            s["opt_exit"] = round(price, 2)
        s["opt_pnl"] = round((s.get("opt_realized") or 0) + (price - entry) * qty_after, 2)
        return True

    # ------------------------------------------------------------------ #
    async def manage_open_positions(self, feed) -> List[Dict]:
        """
        Walk every live signal: bank scale-outs, trail the stop, and close on
        SL, TP3, a Supertrend flip, or the square-off bell.
        """
        with session_scope() as db:
            open_rows = db.query(Signal).filter(Signal.status.in_(["OPEN", "RUNNING"])).all()
            live = [_as_dict(r) for r in open_rows]

        if not live:
            return []

        symbols = sorted({s["symbol"] for s in live})
        prices, trends = {}, {}
        for symbol in symbols:
            try:
                # Entries come from futures candles, so exits must price the same
                # instrument or the P&L is measuring two different things.
                trail_tf = cfg.POS_TRAIL_TIMEFRAME if cfg.is_positional else cfg.TIMEFRAME
                df = await feed.get_historical(symbol, trail_tf, max(60, cfg.ST_PERIOD * 5),
                                               use_futures=True)
                prices[symbol] = float(df["close"].iloc[-1])
                if cfg.USE_SUPERTREND_EXIT:
                    line, direction = supertrend(df, cfg.ST_PERIOD, cfg.ST_MULT)
                    trends[symbol] = (float(line.iloc[-1]), int(direction.iloc[-1]))
            except Exception as exc:
                logger.warning("[Engine] Cannot price %s this cycle: %s", symbol, exc)

        opt_quotes: Dict[int, Optional[float]] = {}
        if cfg.OPTION_TRACKING:
            for snap in live:
                if snap.get("opt_entry"):
                    opt_quotes[snap["id"]] = await self._quote_option(feed, snap)

        square_off = MarketClock.should_square_off() and not cfg.is_positional
        updates = []
        for snap in live:
            ltp = prices.get(snap["symbol"])
            if ltp is None:
                continue
            # A row written by an older schema, or a crash mid-insert, can be
            # missing its levels. Skip it loudly instead of taking down the
            # whole management cycle for every other position.
            missing = [k for k in ("entry", "sl", "tp1", "tp2", "tp3")
                       if snap.get(k) is None]
            if missing:
                logger.warning("[Engine] Signal %s (%s) missing %s - skipping management",
                               snap.get("id"), snap.get("symbol"), ", ".join(missing))
                continue
            exit_reason = "Closed at the intraday square-off" if square_off else None
            if cfg.is_positional:
                exit_reason = self._positional_exit_reason(snap)
            qty_before = snap["qty_open"] if snap.get("qty_open") is not None else (snap.get("qty") or 0)
            changed = self._apply_rules(snap, ltp, trends.get(snap["symbol"]),
                                        exit_reason is not None, exit_reason)
            if snap["id"] in opt_quotes:
                changed = self._update_option(snap, opt_quotes[snap["id"]], qty_before) or changed
            if changed:
                updates.append(snap)

        if updates:
            with session_scope() as db:
                for snap in updates:
                    row = db.get(Signal, snap["id"])
                    if not row:
                        continue
                    for key in ("status", "tp1_hit", "tp2_hit", "qty_open", "trail_sl",
                                "realized_pnl", "exit_price", "exit_time", "pnl",
                                "pnl_pct", "r_multiple", "mfe", "notes",
                                "opt_ltp_now", "opt_updated_at", "opt_exit", "opt_realized",
                                "opt_pnl", "opt_qty_open"):
                        setattr(row, key, snap.get(key))

            for snap in updates:
                if snap["status"] in ("TP3", "SL", "TRAIL", "SQUAREOFF") and self.notifier:
                    await self.notifier.send_exit(snap)
        return updates

    # ------------------------------------------------------------------ #
    @staticmethod
    def _positional_exit_reason(s: Dict, now=None) -> Optional[str]:
        """
        Time-based exits for positional trades. Returns a reason when the
        position must close now, else None.

        - Held POS_MAX_HOLD_DAYS trading days after the entry day: close at
          POS_EXIT_TIME on that day (e.g. entry Monday, 3 days -> Thursday).
        - Option expiry: close at POS_EXIT_TIME, POS_EXIT_DAYS_BEFORE_EXPIRY
          days before the contract's expiry.
        A deadline that passed while the worker was down closes immediately.
        """
        now = now or now_naive()
        exit_t = _parse_time(cfg.POS_EXIT_TIME, time(15, 20))
        at_bell = now.time() >= exit_t

        opened = s.get("timestamp")
        if opened is not None:
            held = MarketClock.trading_days_between(pd.Timestamp(opened).date(), now.date())
            if held > cfg.POS_MAX_HOLD_DAYS or (held == cfg.POS_MAX_HOLD_DAYS and at_bell):
                return f"Max holding period of {cfg.POS_MAX_HOLD_DAYS} trading days reached"

        expiry = s.get("option_expiry")
        if expiry:
            exp = pd.Timestamp(expiry).date()
            due = exp - timedelta(days=cfg.POS_EXIT_DAYS_BEFORE_EXPIRY)
            if now.date() > due or (now.date() == due and at_bell):
                return f"Closed before option expiry ({exp:%d-%b})"
        return None

    def _apply_rules(self, s: Dict, ltp: float, trend, square_off: bool,
                     reason: Optional[str] = None) -> bool:
        long_side = s["direction"] == "LONG"
        sign = 1 if long_side else -1
        entry = s["entry"]
        qty_total = s["qty"] or 0
        changed = False

        # Best excursion, for post-trade review
        best = s.get("mfe") or entry
        s["mfe"] = max(best, ltp) if long_side else min(best, ltp)

        f1, f2, _ = cfg.SCALE_OUT
        q1 = int(qty_total * f1)
        q2 = int(qty_total * f2)

        # --- TP1: bank a third, stop to breakeven -------------------------
        if not s["tp1_hit"] and _reached(ltp, s["tp1"], long_side) and q1 > 0:
            s["realized_pnl"] = (s["realized_pnl"] or 0) + self._leg_pnl(entry, s["tp1"], q1, sign)
            s["qty_open"] = max(0, (s["qty_open"] or qty_total) - q1)
            s["tp1_hit"] = True
            s["status"] = "RUNNING"
            if cfg.MOVE_SL_TO_BE_AFTER_TP1:
                s["trail_sl"] = entry
            s["notes"] = "TP1 banked, stop at breakeven"
            changed = True

        # --- TP2: bank another third, stop to TP1 --------------------------
        if s["tp1_hit"] and not s["tp2_hit"] and _reached(ltp, s["tp2"], long_side) and q2 > 0:
            s["realized_pnl"] = (s["realized_pnl"] or 0) + self._leg_pnl(entry, s["tp2"], q2, sign)
            s["qty_open"] = max(0, (s["qty_open"] or qty_total) - q2)
            s["tp2_hit"] = True
            s["trail_sl"] = s["tp1"]
            s["notes"] = "TP2 banked, stop at TP1"
            changed = True

        # --- Supertrend trail on the runner ---------------------------------
        if cfg.USE_SUPERTREND_EXIT and trend:
            st_line, st_dir = trend
            current = s["trail_sl"] if s["trail_sl"] is not None else s["sl"]
            if long_side and st_dir == 1:
                moved = max(current, round(st_line, 2))
            elif not long_side and st_dir == -1:
                moved = min(current, round(st_line, 2))
            else:
                moved = current
            if moved != current:          # only write when the stop actually moves
                s["trail_sl"] = moved
                changed = True

        # --- Exits ------------------------------------------------------------
        exit_price, status = None, None
        stop = s["trail_sl"] if s["trail_sl"] is not None else s["sl"]

        if _reached(ltp, s["tp3"], long_side):
            exit_price, status = s["tp3"], "TP3"
        elif (long_side and ltp <= stop) or (not long_side and ltp >= stop):
            exit_price = stop
            status = "TRAIL" if (s["tp1_hit"] or stop != s["sl"]) else "SL"
        elif cfg.USE_SUPERTREND_EXIT and trend and ((long_side and trend[1] == -1) or
                                                    (not long_side and trend[1] == 1)):
            exit_price, status = ltp, "TRAIL"
            s["notes"] = "Supertrend flipped against the position"
        elif square_off:
            exit_price, status = ltp, "SQUAREOFF"
            s["notes"] = reason or "Closed at the intraday square-off"

        if exit_price is not None:
            remaining = s["qty_open"] if s["qty_open"] is not None else qty_total
            s["realized_pnl"] = (s["realized_pnl"] or 0) + self._leg_pnl(entry, exit_price, remaining, sign)
            s["qty_open"] = 0
            s["exit_price"] = round(exit_price, 2)
            s["exit_time"] = now_naive()
            s["status"] = status
            s["pnl"] = round(s["realized_pnl"], 2)
            risk_per_unit = abs(entry - s["sl"]) or 1e-9
            s["r_multiple"] = round(s["pnl"] / (risk_per_unit * max(qty_total, 1)), 3)
            s["pnl_pct"] = round((s["pnl"] / cfg.ACCOUNT_BALANCE) * 100, 3)
            changed = True
        elif changed:
            # Mark-to-market the open remainder so the dashboard shows something live.
            remaining = s["qty_open"] or 0
            s["pnl"] = round((s["realized_pnl"] or 0) + self._leg_pnl(entry, ltp, remaining, sign), 2)

        return changed

    @staticmethod
    def _leg_pnl(entry: float, exit_price: float, qty: int, sign: int) -> float:
        if qty <= 0:
            return 0.0
        gross = (exit_price - entry) * sign * qty
        cost = (entry + exit_price) * qty * cfg.COST_PCT_PER_SIDE + cfg.BROKERAGE_PER_LEG
        return gross - cost

    # ------------------------------------------------------------------ #
    async def force_square_off(self, feed, reason: str = "Session close") -> List[Dict]:
        """Close everything still open, used at 15:20 and on shutdown."""
        with session_scope() as db:
            rows = db.query(Signal).filter(Signal.status.in_(["OPEN", "RUNNING"])).all()
            live = [_as_dict(r) for r in rows]
        if not live:
            return []

        closed = []
        for snap in live:
            try:
                quote = await feed.get_live_quote(snap["symbol"], use_futures=True)
                ltp = float(quote["ltp"])
            except Exception as exc:
                logger.warning("[Engine] Square-off price missing for %s: %s", snap["symbol"], exc)
                continue

            sign = 1 if snap["direction"] == "LONG" else -1
            remaining = snap["qty_open"] or snap["qty"]
            opt_price = await self._quote_option(feed, snap) if cfg.OPTION_TRACKING else None
            snap["realized_pnl"] = (snap["realized_pnl"] or 0) + self._leg_pnl(snap["entry"], ltp, remaining, sign)
            snap.update({
                "qty_open": 0, "exit_price": round(ltp, 2), "exit_time": now_naive(),
                "status": "SQUAREOFF", "pnl": round(snap["realized_pnl"], 2), "notes": reason,
            })
            risk = abs(snap["entry"] - snap["sl"]) or 1e-9
            snap["r_multiple"] = round(snap["pnl"] / (risk * max(snap["qty"], 1)), 3)
            snap["pnl_pct"] = round((snap["pnl"] / cfg.ACCOUNT_BALANCE) * 100, 3)
            self._update_option(snap, opt_price, remaining)
            closed.append(snap)

        with session_scope() as db:
            for snap in closed:
                row = db.get(Signal, snap["id"])
                if row:
                    for key in ("status", "qty_open", "realized_pnl", "exit_price",
                                "exit_time", "pnl", "pnl_pct", "r_multiple", "notes",
                                "opt_ltp_now", "opt_updated_at", "opt_exit", "opt_realized",
                                "opt_pnl", "opt_qty_open"):
                        setattr(row, key, snap.get(key))

        logger.info("[Engine] Squared off %d open position(s)", len(closed))
        return closed

    # ------------------------------------------------------------------ #
    @staticmethod
    def daily_stats(timeframe: str = None) -> Dict:
        tf = timeframe or cfg.TIMEFRAME
        with session_scope() as db:
            rows = (db.query(Signal)
                      .filter(Signal.timeframe == tf, Signal.timestamp >= today_start())
                      .all())
            closed = [r for r in rows if r.status in ("TP3", "SL", "TRAIL", "SQUAREOFF")]
            wins = [r for r in closed if (r.pnl or 0) > 0]
            return {
                "total": len(rows),
                "open": len([r for r in rows if r.status in ("OPEN", "RUNNING")]),
                "closed": len(closed),
                "wins": len(wins),
                "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
                "pnl": round(sum(r.pnl or 0 for r in closed), 2),
                "avg_r": round(sum(r.r_multiple or 0 for r in closed) / len(closed), 2) if closed else 0.0,
            }


def _reached(ltp: float, level: Optional[float], long_side: bool) -> bool:
    """A missing level is never 'reached' - it is not a target at zero."""
    if level is None or ltp is None:
        return False
    return ltp >= level if long_side else ltp <= level


def _as_dict(row: Signal) -> Dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}
