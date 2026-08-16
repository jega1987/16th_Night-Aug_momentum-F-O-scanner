"""
Pure functions over an OHLCV DataFrame. No I/O, no state - so every one of
these is unit-testable offline against synthetic candles.

Expected columns: timestamp, open, high, low, close, volume
"""
from typing import Tuple

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
