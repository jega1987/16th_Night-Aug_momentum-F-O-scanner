"""
Scan orchestration: pull candles, score them, log every decision, and hand the
survivors to the signal engine.

Every rejection is written to scan_logs. When the scanner goes quiet for a day
that log is the only way to tell "no setups" apart from "broken feed".
"""
import asyncio
import logging
import pandas as pd
from datetime import timedelta
from typing import Dict, List, Optional

from clock import MarketClock, now_naive, today_start
from config import cfg
from database import IndexSnapshot, OISnapshot, ScanLog, Signal, session_scope
from feed_base import MarketFeed
from filters import FilterEngine
from indicators import atr, bars_since, daily_change_pct, detect_squeeze

logger = logging.getLogger(__name__)


class IndexScanner:
    def __init__(self, feed: MarketFeed, engine=None, store=None,
                 budget=None, universe=None):
        self.feed = feed
        self.engine = engine
        self.store = store
        self.budget = budget
        self.universe = universe
        self.filters = FilterEngine(cfg)
        self.last_scan_at = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    def symbols(self) -> List[str]:
        """Indices always, plus today's ranked equity universe."""
        out = list(cfg.INDICES)
        if cfg.EQUITY_ENABLED and self.universe:
            out += [s for s in self.universe.load() if s not in out]
        return out

    async def scan_all(self, timeframe: str = None, force: bool = False) -> List[Dict]:
        tf = timeframe or cfg.TIMEFRAME

        if cfg.SCAN_ONLY_MARKET_HOURS and not force and not MarketClock.can_take_new_signals():
            logger.debug("[Scanner] Outside the signal window (%s) - skipping",
                         MarketClock.session_label())
            return []

        # Nothing left to act on - don't spend the API budget looking.
        if self.budget and self.budget.exhausted():
            logger.debug("[Scanner] Daily cap reached - scan skipped")
            return []

        watchlist = self.symbols()
        tasks = [self._scan_symbol(sym, tf) for sym in watchlist]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for symbol, outcome in zip(watchlist, outcomes):
            if isinstance(outcome, Exception):
                self.last_error = f"{symbol}: {outcome}"
                logger.error("[Scanner] %s failed: %s", symbol, outcome)
                self._log_scan(symbol, tf, {}, passed=False, reason=f"ERROR: {outcome}")
            elif outcome:
                results.append(outcome)

        self.last_scan_at = now_naive()
        logger.info("[Scanner] %s scan complete - %d setup(s) from %d symbol(s)",
                    tf, len(results), len(watchlist))
        return results

    async def run_cycle(self, timeframe: str = None, force: bool = False) -> List[Dict]:
        """Scan, then persist whatever passes. This is what the scheduler calls."""
        setups = await self.scan_all(timeframe, force=force)
        if not self.engine:
            return setups

        # Strict cap: trim to what the budget allows, highest score first.
        if self.budget:
            setups = self.budget.take(setups)

        created = []
        for setup in setups:
            try:
                signal = await self.engine.create_signal(setup)
                if signal:
                    created.append(signal)
            except Exception as exc:
                logger.exception("[Scanner] Could not record signal for %s: %s", setup["symbol"], exc)

        if self.budget:
            self.budget.notify_if_exhausted()
        return created

    # ------------------------------------------------------------------ #
    async def _scan_symbol(self, symbol: str, timeframe: str) -> Optional[Dict]:
        # ---- stage 1: candles only. Cheap, and it decides everything else. ----
        if self.store and cfg.USE_CANDLE_STORE:
            df = await self.store.get(symbol, timeframe, cfg.HISTORY_BARS, use_futures=True)
        else:
            df = await self.feed.get_historical(symbol, timeframe, cfg.HISTORY_BARS, use_futures=True)

        htf = None
        if cfg.HTF_ALIGNMENT:
            htf = await self._higher_timeframe(symbol, df)

        latest = df.iloc[-1]

        # ---- stage 2: only pay for open interest if the setup is still alive ----
        # Roughly 2% of bars produce a squeeze release, so this skips the OI call
        # for ~98% of symbol-scans. At 200 symbols that is the difference between
        # a workable budget and a rate-limit wall.
        oi = None
        if cfg.USE_OI_FILTER and self._worth_a_second_look(df):
            oi = await self._oi_change(symbol, float(latest["close"]))

        result = self.filters.apply_all(df, htf=htf, oi=oi, symbol=symbol)

        self._log_scan(symbol, timeframe, result.meta,
                       passed=result.passed,
                       reason=result.reason,
                       composite=result.scores.get("composite"))

        if not result.passed:
            logger.debug("[Scanner] %s rejected: %s", symbol, result.reason)
            return None

        blocked = self._cooldown_reason(symbol, result.direction)
        if blocked:
            logger.info("[Scanner] %s %s suppressed: %s", symbol, result.direction, blocked)
            return None

        atr_val = float(atr(df, cfg.ATR_LENGTH).iloc[-1])
        if atr_val <= 0:
            return None

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": result.direction,
            "entry": round(float(latest["close"]), 2),
            "atr": round(atr_val, 2),
            "scores": result.scores,
            "meta": result.meta,
            "composite_score": result.scores.get("composite", 0.0),
            "bar_time": latest["timestamp"],
            "timestamp": now_naive(),
        }

    # ------------------------------------------------------------------ #
    async def _higher_timeframe(self, symbol: str, ltf: pd.DataFrame) -> Optional[pd.DataFrame]:
        """
        Derive the HTF from bars already in hand rather than making a second
        request. Besides saving a call per symbol per cycle, it removes a real
        bug class: independently fetched 5m and 15m series can disagree at the
        boundary, so the trend filter ends up judging a slightly different
        market than the trigger.
        """
        if self.store and cfg.DERIVE_HTF:
            try:
                htf = self.store.resample(ltf, cfg.HTF_TIMEFRAME)
                if len(htf) >= cfg.HTF_EMA_LEN + 2:
                    return htf
                logger.debug("[Scanner] %s: derived HTF too short (%d bars), fetching",
                             symbol, len(htf))
            except Exception as exc:
                logger.warning("[Scanner] %s HTF resample failed: %s", symbol, exc)
        try:
            if self.store and cfg.USE_CANDLE_STORE:
                return await self.store.get(symbol, cfg.HTF_TIMEFRAME, cfg.HTF_BARS)
            return await self.feed.get_historical(symbol, cfg.HTF_TIMEFRAME, cfg.HTF_BARS)
        except Exception as exc:
            logger.warning("[Scanner] %s higher timeframe unavailable: %s", symbol, exc)
            return None

    @staticmethod
    def _worth_a_second_look(df: pd.DataFrame) -> bool:
        """Stage-1 triage: has a squeeze released recently enough to matter?"""
        try:
            _, _, fired = detect_squeeze(df, cfg.BB_LENGTH, cfg.BB_MULT,
                                         cfg.KC_LENGTH, cfg.KC_MULT, cfg.MIN_SQUEEZE_BARS)
            return bars_since(fired) <= cfg.MAX_BARS_SINCE_FIRE
        except Exception:
            return True   # when in doubt, do the full check

    async def _oi_change(self, symbol: str, price: float) -> Optional[Dict]:
        """
        Compare current OI against the previous stored reading. Returns None when
        OI is unavailable so the filter can score it as unknown instead of
        assuming a buildup.
        """
        if not cfg.USE_OI_FILTER:
            return None
        try:
            oi_now = await self.feed.get_oi(symbol)
        except Exception as exc:
            logger.warning("[Scanner] OI fetch failed for %s: %s", symbol, exc)
            return None
        if not oi_now:
            return None

        with session_scope() as db:
            prev = (db.query(OISnapshot)
                      .filter(OISnapshot.symbol == symbol)
                      .order_by(OISnapshot.timestamp.desc())
                      .first())
            db.add(OISnapshot(symbol=symbol, oi=float(oi_now), price=price, timestamp=now_naive()))
            if not prev or not prev.oi:
                return None
            change_pct = ((float(oi_now) - float(prev.oi)) / float(prev.oi)) * 100

        return {"oi": float(oi_now), "prev_oi": float(prev.oi), "change_pct": change_pct}

    def _cooldown_reason(self, symbol: str, direction: str) -> Optional[str]:
        cutoff = now_naive() - timedelta(minutes=cfg.COOLDOWN_BARS * cfg.BAR_MINUTES)
        with session_scope() as db:
            open_count = (db.query(Signal)
                            .filter(Signal.symbol == symbol,
                                    Signal.status.in_(["OPEN", "RUNNING"]))
                            .count())
            if open_count >= cfg.MAX_OPEN_PER_SYMBOL:
                return f"{open_count} position already open"

            recent = (db.query(Signal)
                        .filter(Signal.symbol == symbol,
                                Signal.direction == direction,
                                Signal.timestamp >= cutoff)
                        .count())
            if recent:
                return f"cooldown active ({cfg.COOLDOWN_BARS} bars)"

        # The daily cap is owned by SignalBudget - see run_cycle().
        return None

    @staticmethod
    def _log_scan(symbol: str, timeframe: str, meta: Dict, passed: bool,
                  reason: str, composite: float = None) -> None:
        try:
            with session_scope() as db:
                db.add(ScanLog(
                    symbol=symbol, timeframe=timeframe,
                    in_squeeze=bool(meta.get("in_squeeze", False)),
                    squeeze_bars=int(meta.get("squeeze_bars", 0)),
                    bars_since_fire=int(meta.get("bars_since_fire", 0)),
                    fired=bool(meta.get("bars_since_fire", 9999) == 0),
                    close=meta.get("close"),
                    adx_value=meta.get("adx_value"),
                    rsi_value=meta.get("rsi_value"),
                    vol_ratio=meta.get("vol_ratio"),
                    oi_change_pct=meta.get("oi_change_pct"),
                    composite_score=composite,
                    passed=passed,
                    rejection_reason=None if passed else reason[:250],
                    timestamp=now_naive(),
                ))
        except Exception as exc:
            logger.error("[Scanner] Could not write scan log: %s", exc)

    # ------------------------------------------------------------------ #
    async def get_index_snapshots(self) -> List[Dict]:
        """Live index cards for the dashboard."""
        async def one(symbol: str):
            quote = await self.feed.get_live_quote(symbol, use_futures=False)
            return {
                "symbol": symbol,
                "ltp": quote["ltp"],
                "open_price": quote["open"],
                "high": quote["high"],
                "low": quote["low"],
                "prev_close": quote["prev_close"],
                "change_pct": round(daily_change_pct(quote["ltp"], quote["prev_close"]), 2),
                "change_abs": round(quote["ltp"] - quote["prev_close"], 2),
                "volume": int(quote.get("volume") or 0),
                "oi": int(quote.get("oi") or 0),
            }

        outcomes = await asyncio.gather(*[one(s) for s in cfg.INDICES], return_exceptions=True)
        snapshots = [o for o in outcomes if not isinstance(o, Exception)]
        for symbol, o in zip(cfg.INDICES, outcomes):
            if isinstance(o, Exception):
                logger.warning("[Scanner] Snapshot failed for %s: %s", symbol, o)

        if snapshots:
            with session_scope() as db:
                for snap in snapshots:
                    db.add(IndexSnapshot(timestamp=now_naive(), **snap))
        return snapshots
