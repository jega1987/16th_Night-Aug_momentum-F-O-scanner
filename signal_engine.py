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
from typing import Dict, List, Optional

from clock import MarketClock, now_naive, today_start
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
        """ATR-derived stop and targets, plus a lot-rounded quantity."""
        entry = float(setup["entry"])
        atr_val = float(setup["atr"])
        symbol = setup["symbol"]
        sign = 1 if setup["direction"] == "LONG" else -1

        sl_dist = atr_val * cfg.sl_mult(symbol)
        max_sl_pct = cfg.max_sl_pct(symbol)
        if sl_dist <= 0:
            return None
        if (sl_dist / entry) * 100 > max_sl_pct:
            logger.info("[Engine] %s skipped - stop is %.2f%% away, cap is %.2f%%",
                        symbol, (sl_dist / entry) * 100, max_sl_pct)
            return None

        lot = cfg.lot_size(symbol)
        risk_budget = cfg.ACCOUNT_BALANCE * (cfg.RISK_PER_TRADE_PCT / 100)
        lots = math.floor((risk_budget / sl_dist) / lot)
        if lots < 1:
            logger.info("[Engine] %s skipped - one lot risks more than %.2f%% of the account",
                        symbol, cfg.RISK_PER_TRADE_PCT)
            return None

        qty = lots * lot
        risk_pct = (qty * sl_dist / cfg.ACCOUNT_BALANCE) * 100
        if risk_pct > cfg.MAX_RISK_PER_TRADE_PCT:
            lots = math.floor((cfg.ACCOUNT_BALANCE * cfg.MAX_RISK_PER_TRADE_PCT / 100) / (sl_dist * lot))
            if lots < 1:
                return None
            qty = lots * lot
            risk_pct = (qty * sl_dist / cfg.ACCOUNT_BALANCE) * 100

        strike = atm_strike(entry, cfg.strike_step(symbol))
        opt_type = "CE" if setup["direction"] == "LONG" else "PE"

        return {
            "entry": round(entry, 2),
            "sl": round(entry - sign * sl_dist, 2),
            "tp1": round(entry + sign * atr_val * cfg.ATR_TP1_MULT, 2),
            "tp2": round(entry + sign * atr_val * cfg.ATR_TP2_MULT, 2),
            "tp3": round(entry + sign * atr_val * cfg.ATR_TP3_MULT, 2),
            "qty": qty,
            "lots": lots,
            "risk_pct": round(risk_pct, 3),
            "atm_strike": strike,
            "option_hint": f"{symbol.split()[0]} {strike} {opt_type}",
        }

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
                factor_breakdown={**scores, **setup.get("meta", {})},
                status="OPEN", realized_pnl=0.0, pnl=0.0,
                mfe=levels["entry"],
                triggered_by=setup.get("triggered_by", "scanner"),
            )
            db.add(row)
            db.flush()
            payload = _as_dict(row)

        if plan:
            payload["option_plan"] = plan.to_dict()

        logger.info("[Engine] %s %s @ %.2f | SL %.2f | TP %.2f/%.2f/%.2f | %d qty | %s%s",
                    payload["symbol"], payload["direction"], payload["entry"], payload["sl"],
                    payload["tp1"], payload["tp2"], payload["tp3"], payload["qty"],
                    payload["option_hint"],
                    f" | OPTION BLOCKED: {plan.block_reason}" if plan and plan.blocked else "")

        if self.notifier:
            await self.notifier.send_signal(payload)
        return payload

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
                df = await feed.get_historical(symbol, cfg.TIMEFRAME, max(60, cfg.ST_PERIOD * 5),
                                               use_futures=True)
                prices[symbol] = float(df["close"].iloc[-1])
                if cfg.USE_SUPERTREND_EXIT:
                    line, direction = supertrend(df, cfg.ST_PERIOD, cfg.ST_MULT)
                    trends[symbol] = (float(line.iloc[-1]), int(direction.iloc[-1]))
            except Exception as exc:
                logger.warning("[Engine] Cannot price %s this cycle: %s", symbol, exc)

        square_off = MarketClock.should_square_off()
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
            changed = self._apply_rules(snap, ltp, trends.get(snap["symbol"]), square_off)
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
                                "pnl_pct", "r_multiple", "mfe", "notes"):
                        setattr(row, key, snap[key])

            for snap in updates:
                if snap["status"] in ("TP3", "SL", "TRAIL", "SQUAREOFF") and self.notifier:
                    await self.notifier.send_exit(snap)
        return updates

    # ------------------------------------------------------------------ #
    def _apply_rules(self, s: Dict, ltp: float, trend, square_off: bool) -> bool:
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
            s["notes"] = "Closed at the intraday square-off"

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
            snap["realized_pnl"] = (snap["realized_pnl"] or 0) + self._leg_pnl(snap["entry"], ltp, remaining, sign)
            snap.update({
                "qty_open": 0, "exit_price": round(ltp, 2), "exit_time": now_naive(),
                "status": "SQUAREOFF", "pnl": round(snap["realized_pnl"], 2), "notes": reason,
            })
            risk = abs(snap["entry"] - snap["sl"]) or 1e-9
            snap["r_multiple"] = round(snap["pnl"] / (risk * max(snap["qty"], 1)), 3)
            snap["pnl_pct"] = round((snap["pnl"] / cfg.ACCOUNT_BALANCE) * 100, 3)
            closed.append(snap)

        with session_scope() as db:
            for snap in closed:
                row = db.get(Signal, snap["id"])
                if row:
                    for key in ("status", "qty_open", "realized_pnl", "exit_price",
                                "exit_time", "pnl", "pnl_pct", "r_multiple", "notes"):
                        setattr(row, key, snap[key])

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
