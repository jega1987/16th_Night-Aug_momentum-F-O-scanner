"""
Position sizing, target/stop placement, and trade management.

This is a paper-trading tracker: it records what a trade would have done. It
places no orders. Nothing here is investment advice - size and risk settings
are yours to verify before you act on any of it.

Lifecycle
    OPEN     -> full size live
    RUNNING  -> TP1 (and maybe TP2) banked, remainder trailing
    TP3 | SL | TRAIL-PROFIT | TRAIL-STOP | ST-FLIP | SQUAREOFF -> closed
"""
import logging
import math
from datetime import date, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from clock import MarketClock, _parse_time, now_naive, today_start
from config import cfg
from database import CLOSED_STATUSES, OPEN_STATUSES, Signal, session_scope
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
                opt_bid_now=(plan.bid or None) if (plan and opt.get("opt_entry")) else None,
                opt_ask_now=(plan.ask or None) if (plan and opt.get("opt_entry")) else None,
                opt_updated_at=(now_naive() if opt.get("opt_entry") else None),
                manage_basis=opt.get("manage_basis") or "stock",
                opt_risk=opt.get("opt_risk"), opt_trail_sl=opt.get("opt_trail_sl"),
                opt_token=(plan.scrip_code if plan and opt.get("opt_entry") else None),
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

        try:
            self.after_new_signal()
        except Exception as exc:
            logger.warning("[Engine] Could not start tick tracking for %s: %s", payload["symbol"], exc)
        if self.notifier:
            await self.notifier.send_signal(payload)
        return payload

    # ------------------------------------------------------------------ #
    @staticmethod
    def premium_levels(fill: float, stock_risk: float, delta: float) -> Optional[Dict]:
        """
        Option-managed levels, all on the premium and fixed at entry:
        option R = stock R x |delta|, stop = fill - R, targets = fill +
        TARGET_R_MULTS x R. Exits then fire on the live option price.
        """
        d = abs(float(delta or 0))
        if fill <= 0 or stock_risk <= 0 or d <= 0:
            return None
        r = stock_risk * d
        m1, m2, m3 = cfg.TARGET_R_MULTS
        return {"opt_risk": round(r, 2), "opt_sl": round(max(0.05, fill - r), 2),
                "opt_tp1": round(fill + m1 * r, 2), "opt_tp2": round(fill + m2 * r, 2),
                "opt_tp3": round(fill + m3 * r, 2)}

    @classmethod
    def _option_levels(cls, plan, levels: Dict) -> Dict:
        """
        Option premium at the entry, stop and targets. The entry is the live
        best ask (what a market buy pays).

        OPTION_MANAGED (default): stop/targets are premium levels from the
        live entry and the live delta (premium_levels) and the trade is run on
        the option price. Without a delta, falls back to the old Black-76
        estimate and the trade stays stock-managed.
        """
        if (not plan or not plan.ltp or not plan.expiry
                or not plan.strike or plan.option_type not in ("CE", "PE")):
            return {}
        fill = float(plan.ltp)
        if cfg.OPTION_FILL_AT_TOUCH and plan.ask and float(plan.ask) > 0:
            fill = float(plan.ask)
        if cfg.OPTION_MANAGED and getattr(plan, "delta", None):
            lv = cls.premium_levels(fill, abs(float(levels["entry"]) - float(levels["sl"])), plan.delta)
            if lv:
                return {"opt_entry": round(fill, 2), **lv, "opt_trail_sl": lv["opt_sl"],
                        "manage_basis": "option"}
        if not plan.iv:
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
                return round(max(0.05, fill + black76_price(float(level), k, t, iv, is_call) - base), 2)

            return {"opt_entry": round(fill, 2), "opt_sl": at(levels["sl"]),
                    "opt_tp1": at(levels["tp1"]), "opt_tp2": at(levels["tp2"]),
                    "opt_tp3": at(levels["tp3"]), "manage_basis": "stock"}
        except Exception as exc:
            logger.warning("[Engine] Option levels unavailable for %s: %s", plan.symbol, exc)
            return {}

    @staticmethod
    async def _quote_option(feed, s: Dict) -> Optional[Dict]:
        """
        Current quote of the signal's contract: last traded price, best bid
        and ask, and the mid. None when Kite returns nothing - that is logged,
        and the dashboard shows the last price as stale instead of pretending
        it is live.
        """
        if not (s.get("opt_entry") and s.get("option_expiry") and s.get("atm_strike")
                and s.get("option_type")):
            return None
        label = f"{s.get('symbol')} {s.get('atm_strike')} {s.get('option_type')}"
        try:
            exp = date.fromisoformat(str(s["option_expiry"])[:10])
            q = await feed.get_option_quote(s["symbol"], {"date": exp}, int(s["atm_strike"]),
                                            s["option_type"])
        except Exception as exc:
            logger.warning("[Engine] Option quote failed for %s: %s", label, exc)
            return None
        if not q or not float(q.get("ltp") or 0) > 0:
            logger.warning("[Engine] No option quote for %s - keeping the last price", label)
            return None
        return q

    @staticmethod
    def _sell_price(q: Dict) -> float:
        """What selling the option now would fetch: the best bid, else the mid."""
        bid = float(q.get("bid") or 0)
        if cfg.OPTION_FILL_AT_TOUCH and bid > 0:
            return bid
        return float(q["ltp"])

    @classmethod
    def _update_option(cls, s: Dict, q, qty_before: int) -> bool:
        """
        Follow the stock-level scale-outs with the option: quantity closed in
        this cycle is booked at the current sell price (best bid); the rest is
        marked to it. Buying a CE (LONG) or PE (SHORT), so P&L is always
        premium change x qty. Without a fresh quote the last sell price is
        reused and opt_updated_at is left alone, so its age shows on screen.
        """
        entry = s.get("opt_entry")
        if not entry:
            return False
        if isinstance(q, (int, float)):            # a bare price (older callers, tests)
            q = {"ltp": float(q), "last_price": float(q)}
        if q:
            price = cls._sell_price(q)
            s["opt_last_now"] = round(float(q.get("last_price") or q["ltp"]), 2)
            s["opt_bid_now"] = round(float(q["bid"]), 2) if q.get("bid") else None
            s["opt_ask_now"] = round(float(q["ask"]), 2) if q.get("ask") else None
            s["opt_ltp_now"] = round(price, 2)
            s["opt_updated_at"] = now_naive()
        else:
            price = s.get("opt_ltp_now") or entry       # no fresh quote: last known
        qty_after = s.get("qty_open") or 0
        closed_qty = max(0, (qty_before or 0) - qty_after)
        if closed_qty:
            s["opt_realized"] = round((s.get("opt_realized") or 0) + (price - entry) * closed_qty, 2)
        s["opt_qty_open"] = qty_after
        if s.get("status") in CLOSED_STATUSES:
            s["opt_exit"] = round(price, 2)
            s["opt_exit_at"] = now_naive()
        s["opt_pnl"] = round((s.get("opt_realized") or 0) + (price - entry) * qty_after, 2)
        return True

    # ------------------------------------------------------------------ #
    # Position management
    #
    # Two drivers share one set of rules:
    #   - manage_open_positions(), every minute: reloads open trades from the
    #     database, recomputes the 15-minute Supertrend, refreshes prices by
    #     REST when no fresh tick exists, and applies time exits;
    #   - on_ticks(), every streamed tick (Kite WebSocket): updates the cached
    #     option / futures price and re-checks the affected trades at once.
    # Rule evaluation is synchronous and runs under one asyncio lock, so the
    # two drivers never act on the same trade at the same time.
    # ------------------------------------------------------------------ #
    PERSIST_KEYS = ("status", "tp1_hit", "tp2_hit", "qty_open", "trail_sl",
                    "realized_pnl", "exit_price", "exit_time", "pnl",
                    "pnl_pct", "r_multiple", "mfe", "notes",
                    "opt_ltp_now", "opt_updated_at", "opt_exit", "opt_realized",
                    "opt_pnl", "opt_qty_open", "opt_last_now", "opt_bid_now",
                    "opt_ask_now", "opt_exit_at", "manage_basis", "opt_risk",
                    "opt_trail_sl", "opt_token", "opt_sl", "opt_tp1", "opt_tp2", "opt_tp3")
    TICK_FRESH_SEC = 20

    def _state(self):
        if not hasattr(self, "_pos"):
            import asyncio
            self._pos: Dict[int, Dict] = {}          # open trades by id
            self._lock = asyncio.Lock()
            self._trends: Dict[str, tuple] = {}      # symbol -> (supertrend line, direction)
            self._stock_px: Dict[str, float] = {}    # symbol -> last futures price
            self._opt_q: Dict[int, Dict] = {}        # option token -> latest quote
            self._last_persist: Dict[int, float] = {}
            self._tick_batch: Dict[int, Dict] = {}
            self._drain_pending = False
            self.feed = None
            self.tick_events = 0
        return self

    # ---- loading -------------------------------------------------------- #
    def _load_positions(self) -> None:
        self._state()
        with session_scope() as db:
            rows = db.query(Signal).filter(Signal.status.in_(list(OPEN_STATUSES))).all()
            live = [_as_dict(r) for r in rows]
        fresh = {}
        adopted = []
        for s in live:
            if s.get("manage_basis") is None:
                self._adopt(s)
                adopted.append(s)
            fresh[s["id"]] = s
        self._pos = fresh
        for s in adopted:
            self._persist(s)

    def _adopt(self, s: Dict) -> None:
        """
        A trade opened before option management existed: give it premium
        levels from its own entry premium and the delta recorded at entry, so
        it is run the same way as new trades. Without a delta it stays on
        stock prices.
        """
        lv = None
        if cfg.OPTION_MANAGED and s.get("opt_entry") and s.get("option_delta") \
                and s.get("entry") and s.get("sl"):
            lv = self.premium_levels(float(s["opt_entry"]), abs(float(s["entry"]) - float(s["sl"])),
                                     s["option_delta"])
        if not lv:
            s["manage_basis"] = "stock"
            return
        s.update(lv)
        stop = lv["opt_sl"]
        if s.get("tp1_hit"):
            stop = max(stop, float(s["opt_entry"]))
        if s.get("tp2_hit"):
            stop = max(stop, lv["opt_tp1"])
        s["opt_trail_sl"] = stop
        s["manage_basis"] = "option"
        logger.info("[Engine] %s #%s now option-managed: premium SL %.2f, T %.2f/%.2f/%.2f "
                    "(R %.2f = stock R x delta %.2f)", s["symbol"], s["id"], stop,
                    lv["opt_tp1"], lv["opt_tp2"], lv["opt_tp3"], lv["opt_risk"],
                    abs(float(s["option_delta"])))

    def _persist(self, s: Dict) -> None:
        import time as _t
        with session_scope() as db:
            row = db.get(Signal, s["id"])
            if row:
                for key in self.PERSIST_KEYS:
                    if key in s:
                        setattr(row, key, s.get(key))
        self._last_persist[s["id"]] = _t.monotonic()

    def _token_for(self, feed, s: Dict) -> Optional[int]:
        if s.get("opt_token"):
            return int(s["opt_token"])
        finder = getattr(feed, "option_token", None)
        if not finder or not s.get("option_expiry") or not s.get("atm_strike"):
            return None
        try:
            tok = finder(s["symbol"], date.fromisoformat(str(s["option_expiry"])[:10]),
                         int(s["atm_strike"]), s["option_type"])
        except Exception:
            tok = None
        if tok:
            s["opt_token"] = int(tok)
        return s.get("opt_token")

    def _track(self, feed) -> None:
        """Stream the option contracts and futures of every open trade."""
        tracker = getattr(feed, "track_positions", None)
        if not tracker or not cfg.TICK_MANAGEMENT:
            return
        tokens = [int(s["opt_token"]) for s in self._pos.values() if s.get("opt_token")]
        symbols = sorted({s["symbol"] for s in self._pos.values()})
        try:
            tracker(tokens, symbols)
        except Exception as exc:
            logger.warning("[Engine] Could not stream open positions: %s", exc)

    def after_new_signal(self) -> None:
        """Pick a new trade up immediately instead of at the next minute."""
        self._state()
        self._load_positions()
        if self.feed is not None:
            for s in self._pos.values():
                self._token_for(self.feed, s)
            self._track(self.feed)

    # ---- the one-minute pass -------------------------------------------- #
    async def manage_open_positions(self, feed) -> List[Dict]:
        """
        Every minute: reload open trades, recompute the Supertrend trail,
        refresh any price that has had no tick, then apply the rules
        (including time exits). Ticks do the same between these passes.
        """
        import time as _t
        self._state()
        self.feed = feed
        async with self._lock:
            self._load_positions()
        live = list(self._pos.values())
        if not live:
            return []
        for s in live:
            self._token_for(feed, s)
        self._track(feed)

        trail_tf = cfg.POS_TRAIL_TIMEFRAME if cfg.is_positional else cfg.TIMEFRAME
        for symbol in sorted({s["symbol"] for s in live}):
            try:
                # Entries come from futures candles, so exits must price the same
                # instrument or the P&L is measuring two different things.
                df = await feed.get_historical(symbol, trail_tf, max(60, cfg.ST_PERIOD * 5),
                                               use_futures=True)
                self._stock_px[symbol] = float(df["close"].iloc[-1])
                if cfg.USE_SUPERTREND_EXIT:
                    line, direction = supertrend(df, cfg.ST_PERIOD, cfg.ST_MULT)
                    self._trends[symbol] = (float(line.iloc[-1]), int(direction.iloc[-1]))
            except Exception as exc:
                logger.warning("[Engine] Cannot price %s this cycle: %s", symbol, exc)

        if cfg.OPTION_TRACKING:
            now_m = _t.monotonic()
            for s in live:
                if not s.get("opt_entry"):
                    continue
                tok = s.get("opt_token")
                q = self._opt_q.get(tok) if tok else None
                if q and now_m - q.get("_mono", 0) <= self.TICK_FRESH_SEC:
                    continue                                   # ticks are flowing
                q = await self._quote_option(feed, s)
                if q:
                    q = dict(q, _mono=_t.monotonic(), _at=now_naive())
                    self._opt_q[tok or -s["id"]] = q
                    if not tok:
                        s["_q_key"] = -s["id"]

        updates = []
        async with self._lock:
            for sid in list(self._pos):
                s = self._pos[sid]
                if self._step(s, full=True):
                    updates.append(s)
        await self._after_step(updates)
        return updates

    # ---- ticks ---------------------------------------------------------- #
    def on_ticks(self, batch: List[Dict]) -> None:
        """
        Called on the event loop for every batch the WebSocket delivers.
        Keeps only the latest tick per instrument and schedules one drain.
        """
        import asyncio
        self._state()
        for t in batch:
            self._tick_batch[int(t["token"])] = t
        if not self._drain_pending:
            self._drain_pending = True
            asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        import time as _t
        try:
            while self._tick_batch:
                batch, self._tick_batch = self._tick_batch, {}
                touched = set()
                for tok, t in batch.items():
                    if t.get("symbol"):                       # futures tick
                        self._stock_px[t["symbol"]] = float(t["last_price"])
                        touched |= {sid for sid, s in self._pos.items()
                                    if s["symbol"] == t["symbol"]
                                    and s.get("manage_basis") != "option"}
                    else:                                      # option tick
                        self._opt_q[tok] = {"ltp": t.get("mid") or t["last_price"],
                                            "last_price": t["last_price"],
                                            "bid": t.get("bid"), "ask": t.get("ask"),
                                            "_mono": _t.monotonic(), "_at": t.get("ts") or now_naive()}
                        touched |= {sid for sid, s in self._pos.items() if s.get("opt_token") == tok}
                if not touched:
                    continue
                updates = []
                async with self._lock:
                    for sid in touched:
                        s = self._pos.get(sid)
                        if s and self._step(s, full=False):
                            updates.append(s)
                    self.tick_events += len(touched)
                await self._after_step(updates)
        finally:
            self._drain_pending = False

    # ---- shared ----------------------------------------------------------- #
    def _step(self, s: Dict, full: bool) -> bool:
        """
        Apply the rules to one trade with the cached prices. Returns True when
        something worth writing happened (target, stop move, exit). The
        mark-to-market is written at most every TICK_PERSIST_SEC.
        """
        import time as _t
        missing = [k for k in ("entry", "sl", "tp1", "tp2", "tp3") if s.get(k) is None]
        if missing:
            if full:
                logger.warning("[Engine] Signal %s (%s) missing %s - skipping management",
                               s.get("id"), s.get("symbol"), ", ".join(missing))
            return False
        reason = None
        if cfg.is_positional:
            reason = self._positional_exit_reason(s)
        elif MarketClock.should_square_off():
            reason = "Closed at the intraday square-off"
        trend = self._trends.get(s["symbol"])
        stock_px = self._stock_px.get(s["symbol"])
        tok = s.get("opt_token") or s.get("_q_key")
        q = self._opt_q.get(tok) if tok else None

        if s.get("manage_basis") == "option" and s.get("opt_entry"):
            if not q:
                return False
            event = self._apply_option_rules(s, q, stock_px, trend, reason)
        else:
            if stock_px is None:
                return False
            qty_before = s["qty_open"] if s.get("qty_open") is not None else (s.get("qty") or 0)
            event = self._apply_rules(s, stock_px, trend, reason is not None, reason)
            if s.get("opt_entry"):
                self._update_option(s, q, qty_before)
            if s.get("qty_open"):
                sign = 1 if s["direction"] == "LONG" else -1
                s["pnl"] = round((s["realized_pnl"] or 0) +
                                 self._leg_pnl(s["entry"], stock_px, s["qty_open"], sign), 2)
        due = _t.monotonic() - self._last_persist.get(s["id"], 0) >= cfg.TICK_PERSIST_SEC
        if event or due or full:
            self._persist(s)
        return bool(event)

    async def _after_step(self, updates: List[Dict]) -> None:
        for s in updates:
            if s["status"] in CLOSED_STATUSES:
                self._pos.pop(s["id"], None)
                logger.info("[Engine] %s #%s closed %s | stock %.2f | option %s | P&L stock %+.0f, "
                            "option %s", s["symbol"], s["id"], s["status"], s.get("exit_price") or 0,
                            s.get("opt_exit"), s.get("pnl") or 0,
                            f"{s['opt_pnl']:+.0f}" if s.get("opt_pnl") is not None else "-")
                if self.notifier:
                    try:
                        await self.notifier.send_exit(s)
                    except Exception as exc:
                        logger.warning("[Engine] Exit alert failed: %s", exc)

    def _apply_option_rules(self, s: Dict, q: Dict, stock_px: Optional[float], trend,
                            reason: Optional[str] = None) -> bool:
        """
        Option-managed trade. Levels are premiums fixed at entry; the trigger
        is the live option last traded price, the fill is what selling would
        fetch (best bid). The stock leg is booked at the same moments at the
        live futures price, so the stock P&L stays comparable.
        """
        long_side = s["direction"] == "LONG"
        sign = 1 if long_side else -1
        entry = float(s["entry"])
        fill0 = float(s["opt_entry"])
        qty_total = int(s.get("qty") or 0)
        trig = float(q.get("last_price") or q.get("ltp") or 0)
        if trig <= 0:
            return False
        sell = self._sell_price(q)
        px = float(stock_px) if stock_px else float(s.get("exit_price") or entry)

        s["opt_last_now"] = round(trig, 2)
        s["opt_bid_now"] = round(float(q["bid"]), 2) if q.get("bid") else None
        s["opt_ask_now"] = round(float(q["ask"]), 2) if q.get("ask") else None
        s["opt_ltp_now"] = round(sell, 2)
        s["opt_updated_at"] = q.get("_at") or now_naive()
        if stock_px:
            best = s.get("mfe") or entry
            s["mfe"] = max(best, px) if long_side else min(best, px)
        if s.get("qty_open") is None:
            s["qty_open"] = qty_total
        s["realized_pnl"] = s.get("realized_pnl") or 0.0
        s["opt_realized"] = s.get("opt_realized") or 0.0

        f1, f2, _ = cfg.SCALE_OUT
        q1, q2 = int(qty_total * f1), int(qty_total * f2)
        changed = False

        def book(qty):
            qty = min(qty, s["qty_open"])
            s["opt_realized"] = round(s["opt_realized"] + (sell - fill0) * qty, 2)
            s["realized_pnl"] = s["realized_pnl"] + self._leg_pnl(entry, px, qty, sign)
            s["qty_open"] -= qty

        if not s.get("tp1_hit") and trig >= float(s["opt_tp1"]) and q1 > 0:
            book(q1)
            s["tp1_hit"] = True
            s["status"] = "RUNNING"
            if cfg.MOVE_SL_TO_BE_AFTER_TP1:
                s["opt_trail_sl"] = max(float(s.get("opt_trail_sl") or s["opt_sl"]), fill0)
                s["trail_sl"] = entry
            s["notes"] = f"T1 booked at premium {sell:.2f}, stop to entry premium {fill0:.2f}"
            changed = True
        if s.get("tp1_hit") and not s.get("tp2_hit") and trig >= float(s["opt_tp2"]) and q2 > 0:
            book(q2)
            s["tp2_hit"] = True
            s["opt_trail_sl"] = max(float(s.get("opt_trail_sl") or s["opt_sl"]), float(s["opt_tp1"]))
            s["trail_sl"] = s["tp1"]
            s["notes"] = f"T2 booked at premium {sell:.2f}, stop to T1 premium {float(s['opt_tp1']):.2f}"
            changed = True

        flip = False
        if cfg.USE_SUPERTREND_EXIT and trend:
            st_line, st_dir = trend
            agrees = (long_side and st_dir == 1) or (not long_side and st_dir == -1)
            flip = (long_side and st_dir == -1) or (not long_side and st_dir == 1)
            delta = abs(float(s.get("option_delta") or 0))
            if agrees and delta > 0:
                # The Supertrend line as a premium: entry premium plus delta x
                # the stock move from entry to the line. The stop only tightens.
                cand = round(fill0 + delta * sign * (float(st_line) - entry), 2)
                cur = float(s.get("opt_trail_sl") or s["opt_sl"])
                if cand > cur + 1e-9:
                    s["opt_trail_sl"] = cand
                    stock_cur = s.get("trail_sl") if s.get("trail_sl") is not None else s["sl"]
                    s["trail_sl"] = (max(stock_cur, round(st_line, 2)) if long_side
                                     else min(stock_cur, round(st_line, 2)))
                    changed = True

        stop = float(s.get("opt_trail_sl") or s["opt_sl"])
        status = None
        if trig >= float(s["opt_tp3"]):
            status = "TP3"
            s["notes"] = f"T3 premium {float(s['opt_tp3']):.2f} reached"
        elif trig <= stop:
            if s.get("tp1_hit"):
                status = "TRAIL-PROFIT"
            elif stop > float(s["opt_sl"]) + 1e-9:
                status = "TRAIL-STOP"
            else:
                status = "SL"
            s["notes"] = f"Premium {trig:.2f} hit stop {stop:.2f}"
        elif flip:
            status = "ST-FLIP"
            s["notes"] = "Supertrend flipped against the position"
        elif reason:
            status = "SQUAREOFF"
            s["notes"] = reason

        if status:
            remaining = s["qty_open"]
            book(remaining)
            s["qty_open"] = 0
            s["status"] = status
            s["exit_price"] = round(px, 2)
            s["exit_time"] = now_naive()
            s["opt_exit"] = round(sell, 2)
            s["opt_exit_at"] = now_naive()
            s["pnl"] = round(s["realized_pnl"], 2)
            risk = abs(entry - float(s["sl"])) or 1e-9
            s["r_multiple"] = round(s["pnl"] / (risk * max(qty_total, 1)), 3)
            s["pnl_pct"] = round((s["pnl"] / cfg.ACCOUNT_BALANCE) * 100, 3)
            s["opt_pnl"] = round(s["opt_realized"], 2)
            s["opt_qty_open"] = 0
            return True

        s["opt_qty_open"] = s["qty_open"]
        s["opt_pnl"] = round(s["opt_realized"] + (sell - fill0) * s["qty_open"], 2)
        s["pnl"] = round(s["realized_pnl"] + self._leg_pnl(entry, px, s["qty_open"], sign), 2)
        return changed

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
            if s["tp1_hit"]:
                status = "TRAIL-PROFIT"
            elif stop != s["sl"]:
                status = "TRAIL-STOP"
            else:
                status = "SL"
        elif cfg.USE_SUPERTREND_EXIT and trend and ((long_side and trend[1] == -1) or
                                                    (not long_side and trend[1] == 1)):
            exit_price, status = ltp, "ST-FLIP"
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
            rows = db.query(Signal).filter(Signal.status.in_(list(OPEN_STATUSES))).all()
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
                                "opt_pnl", "opt_qty_open", "opt_last_now", "opt_bid_now",
                                "opt_ask_now", "opt_exit_at"):
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
            closed = [r for r in rows if r.status in CLOSED_STATUSES]
            wins = [r for r in closed if (r.pnl or 0) > 0]
            return {
                "total": len(rows),
                "open": len([r for r in rows if r.status in OPEN_STATUSES]),
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
