"""
Pure functions over an OHLCV DataFrame. No I/O, no state - so every one of
these is unit-testable offline against synthetic candles.

Expected columns: timestamp, open, high, low, close, volume
"""
from typing import List, Tuple

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing - what ATR/ADX/RSI actually use."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return rma(true_range(df), length)


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # avg_loss == 0 means an unbroken run of up bars -> RSI 100
    out = out.where(avg_loss != 0, 100.0)
    return out.fillna(50.0)


def bbands(close: pd.Series, length: int = 20, mult: float = 2.0):
    ma = close.rolling(length).mean()
    std = close.rolling(length).std(ddof=0)
    return ma + mult * std, ma, ma - mult * std


def keltner(df: pd.DataFrame, length: int = 20, mult: float = 1.5):
    """
    Keltner channel using a *rolling mean* of true range, not Wilder smoothing.

    This matters. Bollinger width comes from a rolling 20-bar std, which
    collapses fast when price coils. Wilder's ATR decays slowly, so pairing the
    two leaves the bands permanently nested and the squeeze never registers as
    released. Both sides have to react on the same timescale.
    """
    ma = df["close"].rolling(length).mean()
    rng = true_range(df).rolling(length).mean()
    return ma + mult * rng, ma, ma - mult * rng


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low = df["high"], df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_val = rma(true_range(df), length).replace(0, np.nan)
    plus_di = 100 * rma(plus_dm, length) / atr_val
    minus_di = 100 * rma(minus_dm, length) / atr_val
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    return rma(dx.fillna(0), length)


def detect_squeeze(df: pd.DataFrame, bb_len: int = 20, bb_mult: float = 2.0,
                   kc_len: int = 20, kc_mult: float = 1.5, min_bars: int = 6):
    """
    TTM-style squeeze.

    Returns (in_squeeze, squeeze_duration, fired) where `fired` marks the bar on
    which a squeeze of at least `min_bars` released.
    """
    bb_upper, _, bb_lower = bbands(df["close"], bb_len, bb_mult)
    kc_upper, _, kc_lower = keltner(df, kc_len, kc_mult)

    in_squeeze = ((bb_upper < kc_upper) & (bb_lower > kc_lower)).fillna(False)
    # Consecutive-True counter, reset by every False.
    groups = (~in_squeeze).cumsum()
    squeeze_duration = in_squeeze.astype(int).groupby(groups).cumsum()
    fired = (squeeze_duration.shift(1).fillna(0) >= min_bars) & (~in_squeeze)
    return in_squeeze, squeeze_duration, fired.fillna(False)


def detect_box(df: pd.DataFrame, max_width_atr: float = 2.0, min_bars: int = 6,
               atr_len: int = 75, max_len: int = 40):
    """
    Tight-range box: the longest run of bars ending at each bar whose combined
    high-low range is at most `max_width_atr` x the average true range of the
    previous `atr_len` bars. Catches the flat afternoon coil that follows a
    trending morning - the case Bollinger-inside-Keltner misses, because a
    20-bar Bollinger band keeps the morning trend in its window for hours.

    Same return shape as detect_squeeze: (in_box, duration, fired), where
    in_box needs at least `min_bars` bars and `fired` marks the first bar
    after a qualifying box that breaks it.
    """
    n = len(df)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    ref = true_range(df).rolling(atr_len, min_periods=max(20, atr_len // 3)).mean().shift(1)
    ref = ref.to_numpy(dtype=float)
    length = np.zeros(n, dtype=int)
    for j in range(n):
        if not np.isfinite(ref[j]) or ref[j] <= 0:
            continue
        cap = max_width_atr * ref[j]
        hi, lo, k = highs[j], lows[j], j
        while k - 1 >= 0 and j - k + 1 < max_len:
            nh, nl = max(hi, highs[k - 1]), min(lo, lows[k - 1])
            if nh - nl > cap:
                break
            hi, lo, k = nh, nl, k - 1
        length[j] = j - k + 1 if hi - lo <= cap else 0
    in_box = pd.Series(length >= min_bars, index=df.index)
    duration = pd.Series(np.where(in_box, length, 0), index=df.index)
    fired = (in_box.shift(1, fill_value=False)) & (~in_box)
    return in_box, duration, fired


def compression_setups(df: pd.DataFrame, cfg) -> List[Tuple[str, pd.Series, pd.Series, pd.Series]]:
    """
    The coil detectors switched on by cfg.SQUEEZE_DETECTOR, in priority order:
      "ttm"    - Bollinger inside Keltner only (the original rule)
      "box"    - tight-range box only
      "either" - TTM first, then the box
    Each item is (name, in_coil, duration, fired).
    """
    mode = (getattr(cfg, "SQUEEZE_DETECTOR", "ttm") or "ttm").lower()
    out = []
    if mode in ("ttm", "either"):
        out.append(("ttm",) + tuple(detect_squeeze(
            df, cfg.BB_LENGTH, cfg.BB_MULT, cfg.KC_LENGTH, cfg.KC_MULT, cfg.MIN_SQUEEZE_BARS)))
    if mode in ("box", "either"):
        out.append(("box",) + tuple(detect_box(
            df, getattr(cfg, "BOX_MAX_WIDTH_ATR", 2.0), cfg.MIN_SQUEEZE_BARS,
            getattr(cfg, "BOX_ATR_BARS", 75), cfg.SQUEEZE_RANGE_MAX_BARS * 2)))
    return out


def bars_since(flags: pd.Series) -> int:
    """Bars since the last True. Returns a large number if never True."""
    idx = np.flatnonzero(flags.to_numpy(dtype=bool))
    if idx.size == 0:
        return 10 ** 6
    return int(len(flags) - 1 - idx[-1])


def squeeze_range(df: pd.DataFrame, in_squeeze: pd.Series, duration: pd.Series,
                  max_bars: int = 20) -> Tuple[float, float]:
    """
    High/low of the compression that just released, measured over at most
    `max_bars` of it. Falls back to a fixed lookback when no squeeze is on
    record.
    """
    sq = in_squeeze.to_numpy(dtype=bool)
    idx = np.flatnonzero(sq)
    if idx.size == 0:
        window = df.iloc[-min(max_bars, len(df)):-1]
    else:
        end = int(idx[-1])
        dur = min(int(duration.iloc[end]) or 1, max_bars)
        start = max(0, end - dur + 1)
        window = df.iloc[start:end + 1]
    if window.empty:
        window = df.iloc[:-1]
    return float(window["high"].max()), float(window["low"].min())


def squeeze_bounds(in_squeeze: pd.Series, duration: pd.Series):
    """
    (start_index, end_index) of the most recent squeeze, as positional
    indices into the frame, or None if no squeeze is on record. Uses the same
    walk as squeeze_range so the two always describe the same coil.
    """
    sq = in_squeeze.to_numpy(dtype=bool)
    idx = np.flatnonzero(sq)
    if idx.size == 0:
        return None
    end = int(idx[-1])
    dur = int(duration.iloc[end]) or 1
    return max(0, end - dur + 1), end


def squeeze_start_timestamp(df: pd.DataFrame, in_squeeze: pd.Series, duration: pd.Series):
    """Timestamp of the first bar of the most recent squeeze, or None."""
    bounds = squeeze_bounds(in_squeeze, duration)
    if bounds is None or "timestamp" not in df.columns:
        return None
    return pd.Timestamp(df["timestamp"].iloc[bounds[0]])


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """
    Supertrend(10, 3). Returns (line, direction) where direction is +1 for an
    uptrend (line sits below price) and -1 for a downtrend.
    """
    n = len(df)
    close = df["close"].to_numpy(dtype=float)
    hl2 = ((df["high"] + df["low"]) / 2.0).to_numpy(dtype=float)
    atr_v = atr(df, period).to_numpy(dtype=float)

    upper = hl2 + multiplier * atr_v
    lower = hl2 - multiplier * atr_v
    f_upper = upper.copy()
    f_lower = lower.copy()

    for i in range(1, n):
        f_upper[i] = upper[i] if (upper[i] < f_upper[i - 1] or close[i - 1] > f_upper[i - 1]) else f_upper[i - 1]
        f_lower[i] = lower[i] if (lower[i] > f_lower[i - 1] or close[i - 1] < f_lower[i - 1]) else f_lower[i - 1]

    line = np.empty(n)
    direction = np.empty(n, dtype=int)
    line[0] = f_upper[0]
    direction[0] = -1
    for i in range(1, n):
        if direction[i - 1] == -1:
            if close[i] > f_upper[i]:
                direction[i], line[i] = 1, f_lower[i]
            else:
                direction[i], line[i] = -1, f_upper[i]
        else:
            if close[i] < f_lower[i]:
                direction[i], line[i] = -1, f_upper[i]
            else:
                direction[i], line[i] = 1, f_lower[i]

    return pd.Series(line, index=df.index), pd.Series(direction, index=df.index)


def swing_points(df: pd.DataFrame, n: int = 3):
    """Fractal swing highs/lows confirmed by n bars on each side."""
    highs, lows = df["high"], df["low"]
    swing_high = highs == highs.rolling(2 * n + 1, center=True).max()
    swing_low = lows == lows.rolling(2 * n + 1, center=True).min()
    return swing_high.fillna(False), swing_low.fillna(False)


def daily_change_pct(close: float, prev_close: float) -> float:
    if prev_close and prev_close > 0:
        return ((close - prev_close) / prev_close) * 100
    return 0.0


def atm_strike(spot: float, step: int) -> int:
    """Round spot to the nearest tradable strike."""
    if not step:
        return int(round(spot))
    return int(round(spot / step) * step)


def bar_done_times(q: pd.DataFrame, session_close=(15, 30)) -> np.ndarray:
    """
    Completion time of each bar of a resampled series (15m, 60m or daily):
    bar start + bar length, capped at the session close (the last hourly bar,
    15:15-15:30, completes at 15:30; a daily bar completes at 15:30).
    """
    ts = pd.to_datetime(q["timestamp"])
    if len(ts) == 0:
        return np.array([], dtype="datetime64[ns]")
    close = ts.dt.normalize() + pd.Timedelta(hours=session_close[0], minutes=session_close[1])
    same_day = ts.dt.normalize() == ts.shift().dt.normalize()
    d = ts.diff()
    steps = d[same_day & (d > pd.Timedelta(0))]
    if steps.empty and len(ts) >= 2:
        return close.values                       # one bar per day: daily bars
    step = steps.min() if not steps.empty else pd.Timedelta(minutes=15)
    return np.minimum(ts + step, close).values


def breakout_states(q: pd.DataFrame, d5: pd.DataFrame, in_sqz: pd.Series, duration: pd.Series,
                    min_bars: int, memory_bars: int, valid_bars: int, max_range_bars: int = 20):
    """
    First-breakout tracking with coil memory (rules of 1 Nov 2026).

    For every 5-minute bar i (closed), using only data available at its close:
      - the coil is the latest run of >= min_bars squeeze bars among the
        15-minute bars COMPLETED BEFORE the 15-minute period that contains
        bar i. The breakout candle's own 15-minute bar is therefore never part
        of the range it is breaking (the JUBLFOOD 30-Oct bug);
      - a coil stays in memory for memory_bars 15-minute bars after its last
        squeeze bar, so a same-day or multi-day coil can still be broken out
        of after the bands have widened;
      - the range (high/low of up to max_range_bars coil bars) is frozen at the
        first 5-minute close outside it; a close back inside resets it;
      - the bar is actionable while it is within valid_bars 5-minute bars of
        that first breakout and still outside the frozen range.

    Returns a DataFrame aligned with d5: side (+1/-1/0), hi, lo, eligible,
    coil_start (15m index), coil_bars, break_i (5m index of first break),
    bars_since_break, state ('none'|'compressing'|'inside'|'breakout'|'stale').
    """
    n5 = len(d5)
    out = {"side": np.zeros(n5, dtype=int), "hi": np.full(n5, np.nan), "lo": np.full(n5, np.nan),
           "eligible": np.zeros(n5, dtype=bool), "coil_start": np.full(n5, -1),
           "coil_bars": np.zeros(n5, dtype=int), "break_i": np.full(n5, -1),
           "bars_since_break": np.full(n5, -1), "state": np.array(["none"] * n5, dtype=object)}
    if n5 == 0 or len(q) == 0:
        return pd.DataFrame(out)
    ts5 = pd.to_datetime(d5["timestamp"]).values
    # number of squeeze-timeframe bars COMPLETED by the 5-minute bar's open,
    # i.e. those before the period that contains it (15m, 60m or daily bars).
    pos = np.searchsorted(bar_done_times(q), ts5, side="right")
    sq = np.asarray(in_sqz, dtype=bool)
    dur = np.asarray(duration).astype(int)
    hi_q, lo_q = q["high"].to_numpy(dtype=float), q["low"].to_numpy(dtype=float)
    # last squeeze bar at or before each q index
    last_true = np.full(len(q), -1)
    lt = -1
    for k in range(len(q)):
        if sq[k]:
            lt = k
        last_true[k] = lt
    closes = d5["close"].to_numpy(dtype=float)
    rng_cache = {}
    cur_key, frozen, first = None, None, None
    for i in range(n5):
        k = pos[i] - 1
        if k < 0:
            cur_key = None
            continue
        e = last_true[k]
        if e < 0 or dur[e] < min_bars or (k - e) > memory_bars:
            # a coil still forming shows as 'compressing'
            if e >= 0 and e == k and dur[e] < min_bars:
                out["state"][i] = "compressing"
                out["coil_bars"][i] = dur[e]
            cur_key, frozen, first = None, None, None
            continue
        s = e - dur[e] + 1
        if (s, e) not in rng_cache:
            a = max(s, e - max_range_bars + 1)
            rng_cache[(s, e)] = (float(hi_q[a:e + 1].max()), float(lo_q[a:e + 1].min()))
        if cur_key != s:                       # a new coil: forget the old breakout
            cur_key, frozen, first = s, None, None
        hi, lo = frozen if frozen else rng_cache[(s, e)]
        c = closes[i]
        side = 1 if c > hi else (-1 if c < lo else 0)
        if side == 0:
            frozen, first = None, None
            hi, lo = rng_cache[(s, e)]
            out["state"][i] = "inside"
        else:
            if first is None or side != first[1]:
                frozen, first = (hi, lo), (i, side)
            since = i - first[0]
            out["break_i"][i] = first[0]
            out["bars_since_break"][i] = since
            out["side"][i] = side
            ok = since <= valid_bars
            out["eligible"][i] = ok
            out["state"][i] = "breakout" if ok else "stale"
        out["hi"][i], out["lo"][i] = hi, lo
        out["coil_start"][i], out["coil_bars"][i] = s, dur[e]
    return pd.DataFrame(out)
