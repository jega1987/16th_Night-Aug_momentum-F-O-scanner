"""
Signal confluence scoring.

Each factor returns a score in [0, 1]. Factors listed in cfg.HARD_FAIL_FACTORS
must score a full 1.0 or the setup is rejected; the rest feed a weighted
composite that has to clear cfg.MIN_COMPOSITE.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import pandas as pd

from indicators import (adx, atr, bars_since, detect_squeeze, ema, rsi,
                        squeeze_range, swing_points)


@dataclass
class FilterResult:
    passed: bool = False
    direction: Optional[str] = None
    reason: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    meta: Dict[str, float] = field(default_factory=dict)


class FilterEngine:
    def __init__(self, config):
        self.cfg = config

    # ------------------------------------------------------------------ #
    def apply_all(self, df: pd.DataFrame, htf: pd.DataFrame = None,
                  oi: Optional[Dict] = None, symbol: str = None) -> FilterResult:
        """
        `symbol` selects the index or equity threshold set. Stocks gap harder,
        trend more cleanly and reverse faster than indices, so they get their
        own volume, ADX and composite bars rather than inheriting index tuning.
        """
        cfg = self.cfg
        res = FilterResult()
        vol_mult = cfg.volume_mult(symbol) if symbol else cfg.VOLUME_MULT
        adx_min = cfg.adx_threshold(symbol) if symbol else cfg.ADX_THRESHOLD
        min_composite = cfg.min_composite(symbol) if symbol else cfg.MIN_COMPOSITE
        res.meta["asset_class"] = ("INDEX" if (symbol and cfg.is_index(symbol)) else "EQUITY")

        min_bars = max(cfg.BB_LENGTH, cfg.KC_LENGTH, cfg.VOLUME_MA_LEN, cfg.ADX_LENGTH) + 10
        if df is None or len(df) < min_bars:
            res.reason = f"Not enough candles ({0 if df is None else len(df)} < {min_bars})"
            return res

        latest = df.iloc[-1]
        in_sqz, sqz_dur, fired = detect_squeeze(
            df, cfg.BB_LENGTH, cfg.BB_MULT, cfg.KC_LENGTH, cfg.KC_MULT, cfg.MIN_SQUEEZE_BARS
        )
        since_fire = bars_since(fired)
        hi, lo = squeeze_range(df, in_sqz, sqz_dur, cfg.SQUEEZE_RANGE_MAX_BARS)

        res.meta.update({
            "in_squeeze": bool(in_sqz.iloc[-1]),
            "squeeze_bars": int(sqz_dur.iloc[-1]),
            "bars_since_fire": int(min(since_fire, 9999)),
            "squeeze_high": round(hi, 2),
            "squeeze_low": round(lo, 2),
            "close": float(latest["close"]),
        })

        # 1. Squeeze must have actually released recently ------------------
        res.scores["squeeze"] = 1.0 if since_fire <= cfg.MAX_BARS_SINCE_FIRE else 0.0
        if res.scores["squeeze"] < 1.0 and "squeeze" in cfg.HARD_FAIL_FACTORS:
            res.reason = ("Still compressing" if in_sqz.iloc[-1]
                          else f"No squeeze release in last {cfg.MAX_BARS_SINCE_FIRE} bars")
            return res

        # 2. Direction: close outside the compression range ----------------
        close = float(latest["close"])
        if close > hi:
            res.direction = "LONG"
            res.scores["direction"] = 1.0
        elif close < lo:
            res.direction = "SHORT"
            res.scores["direction"] = 1.0
        else:
            res.scores["direction"] = 0.0
            res.reason = f"Close {close:.2f} inside squeeze range {lo:.2f}-{hi:.2f}"
            return res
        direction = res.direction

        # 3. Volume expansion (MA excludes the breakout bar itself) --------
        vol_len = cfg.VOLUME_MA_LEN
        vol_hist = df["volume"].iloc[-(vol_len + 1):-1]
        vol_avg = float(vol_hist.mean()) if len(vol_hist) else 0.0
        cur_vol = float(latest["volume"])
        if vol_avg <= 0:
            # Cash indices publish no volume. Don't fake a pass - score neutral
            # and let the composite decide.
            res.scores["volume"] = 0.0 if "volume" not in cfg.HARD_FAIL_FACTORS else 1.0
            vol_ratio = 0.0
        else:
            vol_ratio = cur_vol / vol_avg
            res.scores["volume"] = 1.0 if vol_ratio >= vol_mult else 0.0
        res.meta["vol_ratio"] = round(vol_ratio, 2)

        # 4. ADX trend strength --------------------------------------------
        if cfg.USE_ADX_FILTER:
            adx_val = float(adx(df, cfg.ADX_LENGTH).iloc[-1])
            res.scores["adx"] = 1.0 if adx_val > adx_min else 0.0
        else:
            adx_val = 0.0
            res.scores["adx"] = 1.0
        res.meta["adx_value"] = round(adx_val, 2)

        # 5. RSI confluence -------------------------------------------------
        if cfg.USE_RSI_FILTER:
            rsi_val = float(rsi(df["close"], cfg.RSI_LENGTH).iloc[-1])
            ok = rsi_val > cfg.RSI_LONG_MIN if direction == "LONG" else rsi_val < cfg.RSI_SHORT_MAX
            res.scores["rsi"] = 1.0 if ok else 0.0
        else:
            rsi_val = 50.0
            res.scores["rsi"] = 1.0
        res.meta["rsi_value"] = round(rsi_val, 2)

        # 6. Liquidity sweep -------------------------------------------------
        res.scores["sweep"] = 1.0 if self._check_sweep(df, direction) else 0.0

        # 7. Market structure ------------------------------------------------
        res.scores["structure"] = 1.0 if self._check_structure(df, direction) else 0.0

        # 8. Open interest buildup -------------------------------------------
        res.scores["oi"] = self._score_oi(oi, direction)
        res.meta["oi_change_pct"] = round(float(oi.get("change_pct", 0.0)), 3) if oi else 0.0

        # 9. Higher-timeframe alignment ---------------------------------------
        res.scores["htf"] = self._score_htf(htf, direction, close)

        # Optional second-candle confirmation (off by default - the spec asks
        # for entry on the breakout bar, not one bar later).
        if cfg.USE_SECOND_CANDLE_FILTER and len(df) > 1:
            prev = df.iloc[-2]
            ok = close > prev["close"] if direction == "LONG" else close < prev["close"]
            res.scores["second_candle"] = 1.0 if ok else 0.0

        # ---- verdict --------------------------------------------------------
        # Composite is computed over the SOFT factors only. Hard-fail factors
        # are 1.0 on every recorded signal, so including them just adds a
        # constant and compresses the usable range.
        weights = cfg.FACTOR_WEIGHTS
        soft = {k: w for k, w in weights.items() if k not in cfg.HARD_FAIL_FACTORS}
        soft_total = sum(soft.values()) or 1.0
        composite = sum(res.scores.get(k, 0.0) * w for k, w in soft.items()) / soft_total
        res.scores["composite"] = round(composite, 3)
        res.scores["composite_all"] = round(
            sum(res.scores.get(k, 0.0) * w for k, w in weights.items())
            / (sum(weights.values()) or 1.0), 3)

        hard_fails = [k for k in cfg.HARD_FAIL_FACTORS if res.scores.get(k, 0.0) < 1.0]
        if hard_fails:
            res.reason = "Failed: " + ", ".join(hard_fails)
            return res

        if composite < min_composite:
            res.reason = f"Composite {composite:.2f} < {min_composite:.2f}"
            return res

        res.passed = True
        res.reason = "PASSED"
        return res

    # ------------------------------------------------------------------ #
    def _check_sweep(self, df: pd.DataFrame, direction: str) -> bool:
        """
        A sweep is a wick through a level that was set *before* the candles we
        are inspecting, followed by a close back on the right side of it.

        The reference level therefore has to come from an earlier window than
        the candidate candles, otherwise the test compares a window's low
        against its own minimum and can never be true.
        """
        cand_n = max(1, self.cfg.SWEEP_MAX_BARS)
        ref_n = max(2, self.cfg.SWEEP_LOOKBACK)
        if len(df) < cand_n + ref_n + 1:
            return False

        ref = df.iloc[-(cand_n + ref_n):-cand_n]
        candidates = df.iloc[-cand_n:]

        if direction == "LONG":
            level = float(ref["low"].min())
            for _, bar in candidates.iterrows():
                if bar["low"] < level <= bar["close"]:
                    return True
        else:
            level = float(ref["high"].max())
            for _, bar in candidates.iterrows():
                if bar["high"] > level >= bar["close"]:
                    return True
        return False

    def _check_structure(self, df: pd.DataFrame, direction: str) -> bool:
        """Break of the most recent confirmed swing in the trade's direction."""
        n = self.cfg.SWING_N
        if len(df) < n * 2 + 2:
            return False
        sh, sl = swing_points(df, n)
        # The last n bars can't be confirmed swings yet - ignore them.
        sh, sl = sh.iloc[:-n], sl.iloc[:-n]
        close = float(df.iloc[-1]["close"])

        if direction == "LONG":
            idx = sh[sh].index
            if len(idx) == 0:
                return False
            return close > float(df.loc[idx[-1], "high"])
        idx = sl[sl].index
        if len(idx) == 0:
            return False
        return close < float(df.loc[idx[-1], "low"])

    def _score_oi(self, oi: Optional[Dict], direction: str) -> float:
        """
        Long buildup  = price up   + OI up.
        Short buildup = price down + OI up.
        Rising OI confirms new positions in both directions; falling OI on a
        breakout is short covering / long unwinding, which is weaker.
        """
        if not self.cfg.USE_OI_FILTER:
            return 1.0
        if not oi or oi.get("change_pct") is None:
            return 0.0  # unknown contributes nothing - it is not evidence
        change = float(oi["change_pct"])
        if change >= self.cfg.MIN_OI_CHANGE_PCT:
            return 1.0
        if change <= -self.cfg.MIN_OI_CHANGE_PCT:
            return 0.0
        return 0.5

    def _score_htf(self, htf: pd.DataFrame, direction: str, close: float) -> float:
        if not self.cfg.HTF_ALIGNMENT:
            return 1.0
        if htf is None or len(htf) < self.cfg.HTF_EMA_LEN + 2:
            return 0.0  # no data is not confirmation
        htf_ema = float(ema(htf["close"], self.cfg.HTF_EMA_LEN).iloc[-1])
        htf_close = float(htf["close"].iloc[-1])
        if direction == "LONG":
            return 1.0 if htf_close > htf_ema else 0.0
        return 1.0 if htf_close < htf_ema else 0.0
