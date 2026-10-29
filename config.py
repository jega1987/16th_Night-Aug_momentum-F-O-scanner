"""
Central configuration. Everything is env-driven so the same image runs
locally and on Railway without code changes.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, default)))
    except (TypeError, ValueError):
        return default


def _list(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _floats(key: str, default: List[float]) -> List[float]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return list(default)
    try:
        return [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        return list(default)


def _json(key: str, default: dict) -> dict:
    raw = os.getenv(key, "").strip()
    if not raw:
        return dict(default)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return dict(default)


@dataclass
class StrategyConfig:
    # ---------- universe ----------
    INDICES: List[str] = field(default_factory=lambda: _list("INDICES", ["NIFTY 50", "BANKNIFTY", "SENSEX"]))

    # Lot sizes change with exchange circulars - verify before going live.
    # Override with LOT_SIZES='{"NIFTY 50": 75, "BANKNIFTY": 35, "SENSEX": 20}'
    LOT_SIZES: Dict[str, int] = field(
        default_factory=lambda: _json("LOT_SIZES", {"NIFTY 50": 75, "BANKNIFTY": 35, "SENSEX": 20})
    )
    DEFAULT_LOT_SIZE: int = _int("DEFAULT_LOT_SIZE", 25)

    # Strike step per index, used for ATM option strike suggestion.
    STRIKE_STEPS: Dict[str, int] = field(
        default_factory=lambda: _json("STRIKE_STEPS", {"NIFTY 50": 50, "BANKNIFTY": 100, "SENSEX": 100})
    )
    # Stock options: pick the nearest listed strike that is a multiple of a
    # round step for the price level - {"min_price": step, ...}. Liquidity
    # sits on round strikes (ICICIGI 1500/1600, not 1540/1560).
    ROUND_STRIKES: bool = _bool("ROUND_STRIKES", True)
    ROUND_STRIKE_BANDS: Dict = field(
        default_factory=lambda: _json("ROUND_STRIKE_BANDS",
                                      {"1000": 100, "300": 50, "100": 25, "0": 5})
    )

    # ---------- timeframe ----------
    TIMEFRAME: str = os.getenv("SCAN_TIMEFRAME", "5m")
    HTF_TIMEFRAME: str = os.getenv("HTF_TIMEFRAME", "15m")
    BAR_MINUTES: int = 5          # recomputed in __post_init__
    HISTORY_BARS: int = _int("HISTORY_BARS", 200)
    HTF_BARS: int = _int("HTF_BARS", 120)

    # ---------- squeeze ----------
    BB_LENGTH: int = _int("BB_LENGTH", 20)
    BB_MULT: float = _float("BB_MULT", 2.0)
    KC_LENGTH: int = _int("KC_LENGTH", 20)
    KC_MULT: float = _float("KC_MULT", 1.5)
    MIN_SQUEEZE_BARS: int = _int("MIN_SQUEEZE_BARS", 6)
    # How recently the squeeze must have released for a breakout to count.
    MAX_BARS_SINCE_FIRE: int = _int("MAX_BARS_SINCE_FIRE", 3)
    # A 40-bar coil produces a range so wide the move is over by the time
    # price clears it. Measure the breakout level over the most recent
    # stretch of the compression instead.
    SQUEEZE_RANGE_MAX_BARS: int = _int("SQUEEZE_RANGE_MAX_BARS", 20)
    # Which coil detectors count as a setup: "ttm" (Bollinger inside Keltner,
    # the original rule), "box" (tight-range box) or "either" (TTM first,
    # then the box). The box is a run of >= MIN_SQUEEZE_BARS bars whose
    # combined high-low range is at most BOX_MAX_WIDTH_ATR x the average true
    # range of the previous BOX_ATR_BARS bars (75 x 15m = about 3 sessions).
    SQUEEZE_DETECTOR: str = os.getenv("SQUEEZE_DETECTOR", "ttm").strip().lower()
    # Watch-only setups: evaluated every scan alongside the live rules and
    # recorded in watch_signals (dashboard /watch), never traded. "" = off,
    # "box" = the tight-range box at WATCH_BOX_WIDTH_ATR.
    WATCH_DETECTOR: str = os.getenv("WATCH_DETECTOR", "").strip().lower()
    WATCH_BOX_WIDTH_ATR: float = _float("WATCH_BOX_WIDTH_ATR", 2.5)
    BOX_MAX_WIDTH_ATR: float = _float("BOX_MAX_WIDTH_ATR", 2.0)
    BOX_ATR_BARS: int = _int("BOX_ATR_BARS", 75)

    # ---------- confluence ----------
    SWEEP_LOOKBACK: int = _int("SWEEP_LOOKBACK", 10)
    SWEEP_MAX_BARS: int = _int("SWEEP_MAX_BARS", 3)
    SWING_N: int = _int("SWING_N", 3)
    VOLUME_MULT: float = _float("VOLUME_MULT", 1.5)
    VOLUME_MA_LEN: int = _int("VOLUME_MA_LEN", 20)
    USE_RSI_FILTER: bool = _bool("USE_RSI_FILTER", True)
    RSI_LENGTH: int = _int("RSI_LENGTH", 14)
    RSI_LONG_MIN: float = _float("RSI_LONG_MIN", 60.0)
    RSI_SHORT_MAX: float = _float("RSI_SHORT_MAX", 40.0)
    USE_OI_FILTER: bool = _bool("USE_OI_FILTER", True)
    MIN_OI_CHANGE_PCT: float = _float("MIN_OI_CHANGE_PCT", 0.25)
    USE_ADX_FILTER: bool = _bool("USE_ADX_FILTER", True)
    ADX_LENGTH: int = _int("ADX_LENGTH", 14)
    ADX_THRESHOLD: float = _float("ADX_THRESHOLD", 20.0)
    HTF_ALIGNMENT: bool = _bool("HTF_ALIGNMENT", True)
    HTF_EMA_LEN: int = _int("HTF_EMA_LEN", 20)
    USE_SECOND_CANDLE_FILTER: bool = _bool("USE_SECOND_CANDLE_FILTER", False)

    # Factors that must score 1.0 or the setup is rejected outright.
    # Everything else contributes to the weighted composite only.
    HARD_FAIL_FACTORS: List[str] = field(
        default_factory=lambda: _list("HARD_FAIL_FACTORS", ["direction", "squeeze", "volume", "adx"])
    )
    # Scored over the SOFT factors only, rescaled to 0-1.
    #
    # Previously the composite included the hard-fail factors, which are 1.0 by
    # definition on any signal that got recorded. They contributed a constant
    # 0.60, so a 0.70 gate needed just 25% of the soft factors - RSI alone
    # cleared it. Excluding them makes the number mean what it looks like.
    MIN_COMPOSITE: float = _float("MIN_COMPOSITE", 0.50)

    FACTOR_WEIGHTS: Dict[str, float] = field(
        default_factory=lambda: _json(
            "FACTOR_WEIGHTS",
            {
                "direction": 0.18,
                "squeeze": 0.17,
                "volume": 0.15,
                "adx": 0.10,
                "rsi": 0.10,
                "structure": 0.10,
                "sweep": 0.08,
                "oi": 0.07,
                "htf": 0.05,
            },
        )
    )

    # Exchange holidays, ISO dates, comma-separated. The scanner and the
    # square-off job stay idle on these. Extend it each year from the NSE
    # holiday circular, e.g. MARKET_HOLIDAYS=2026-10-02,2026-10-20,2026-11-05
    MARKET_HOLIDAYS: List[str] = field(default_factory=lambda: _list("MARKET_HOLIDAYS", []))

    # ---------- signal window & bar integrity ----------
    # Entries are allowed between these IST times. 09:45 skips the opening
    # rotation, where a 6-bar squeeze can only exist by spanning the overnight
    # gap; 14:15 leaves a 4R target at least an hour before the 15:20 bell.
    SIGNAL_START_TIME: str = os.getenv("SIGNAL_START_TIME", "09:45")
    SIGNAL_END_TIME: str = os.getenv("SIGNAL_END_TIME", "14:15")
    # Score closed candles only. Kite's historical endpoint and the websocket
    # builder both hand over the bar that is still forming; scanning it means
    # the "breakout close" is a 20-second print and the volume gate compares
    # a sliver of a bar against a 20-bar average of whole ones.
    SCAN_CLOSED_BARS_ONLY: bool = _bool("SCAN_CLOSED_BARS_ONLY", True)
    # The coil must start and end in today's session. Bollinger and Keltner
    # measured across yesterday's close and today's open describe nothing.
    REQUIRE_INTRADAY_SQUEEZE: bool = _bool("REQUIRE_INTRADAY_SQUEEZE", True)

    # ---------- hybrid squeeze (detect on 15m, enter on 5m) ----------
    # "5m": squeeze detected on the scan timeframe, as before.
    # "15m": squeeze and its high/low come from completed 15-minute bars; the
    #        scan still runs every 5 minutes and fires when a closed 5-minute
    #        bar closes outside that 15-minute range and passes the other checks.
    #        MIN_SQUEEZE_BARS and MAX_BARS_SINCE_FIRE then count 15-minute bars.
    SQUEEZE_TIMEFRAME: str = os.getenv("SQUEEZE_TIMEFRAME", "5m").strip().lower()
    # 5-minute bars loaded per symbol in hybrid mode (~6 sessions), so a
    # multi-day 15-minute coil plus BB/KC warm-up fits.
    SQUEEZE_HISTORY_BARS: int = _int("SQUEEZE_HISTORY_BARS", 450)
    # Gap filter, for coils that began on an earlier day: reject when today's
    # open is already more than GAP_MAX_ATR x ATR (squeeze timeframe) beyond
    # the range in the trade's direction.
    GAP_FILTER: bool = _bool("GAP_FILTER", True)
    GAP_MAX_ATR: float = _float("GAP_MAX_ATR", 0.5)
    # Opening volume baseline: until OPEN_BASELINE_UNTIL, compare a bar's
    # volume with the same 5-minute slot averaged over the last
    # OPEN_BASELINE_DAYS sessions, not with yesterday's quiet afternoon.
    OPEN_VOLUME_BASELINE: bool = _bool("OPEN_VOLUME_BASELINE", False)
    OPEN_BASELINE_UNTIL: str = os.getenv("OPEN_BASELINE_UNTIL", "09:45")
    OPEN_BASELINE_DAYS: int = _int("OPEN_BASELINE_DAYS", 10)
    OPEN_BASELINE_MIN_DAYS: int = _int("OPEN_BASELINE_MIN_DAYS", 5)
    # RSI exhaustion cap: no LONG above RSI_LONG_MAX, no SHORT below RSI_SHORT_MIN.
    RSI_EXHAUSTION_FILTER: bool = _bool("RSI_EXHAUSTION_FILTER", False)
    RSI_LONG_MAX: float = _float("RSI_LONG_MAX", 75.0)
    RSI_SHORT_MIN: float = _float("RSI_SHORT_MIN", 25.0)

    # ---------- trading mode ----------
    # "intraday":   everything is squared off at 15:20 (the original design).
    # "positional": positions carry overnight. They close on SL / targets /
    #               trailing stop as usual, or at POS_EXIT_TIME once they have
    #               been held POS_MAX_HOLD_DAYS trading days after the entry
    #               day, or on the day before the option's expiry.
    TRADING_MODE: str = os.getenv("TRADING_MODE", "intraday").strip().lower()
    POS_MAX_HOLD_DAYS: int = _int("POS_MAX_HOLD_DAYS", 3)
    POS_EXIT_TIME: str = os.getenv("POS_EXIT_TIME", "15:20")
    POS_EXIT_DAYS_BEFORE_EXPIRY: int = _int("POS_EXIT_DAYS_BEFORE_EXPIRY", 1)
    # Overnight-sized stop: one more candidate in the stop placement,
    # POS_SL_ATR_MULT x ATR on POS_SL_ATR_TIMEFRAME bars (widest still wins).
    POS_SL_ATR_TIMEFRAME: str = os.getenv("POS_SL_ATR_TIMEFRAME", "60m")
    POS_SL_ATR_MULT: float = _float("POS_SL_ATR_MULT", 1.5)
    # Supertrend trailing stop is computed on this timeframe in positional mode.
    POS_TRAIL_TIMEFRAME: str = os.getenv("POS_TRAIL_TIMEFRAME", "15m")
    # Option expiry: in positional mode pick the nearest expiry at least this
    # many days away, so the option is not decaying fast while held.
    OPTION_MIN_DTE: int = _int("OPTION_MIN_DTE", 7)
    # Cap on positions open at the same time, across days. 0 = no cap.
    MAX_OPEN_POSITIONS: int = _int("MAX_OPEN_POSITIONS", 0)
    # Re-quote each open signal's option every minute and track option P&L.
    OPTION_TRACKING: bool = _bool("OPTION_TRACKING", True)
    # Paper fills at the touch: buy at the best ask, sell at the best bid
    # (what a market order would actually get), instead of the bid/ask mid.
    OPTION_FILL_AT_TOUCH: bool = _bool("OPTION_FILL_AT_TOUCH", True)
    # Run option trades on the premium: stop/targets = fill -/+ multiples of
    # (stock R x |delta|) and exits fire on the live option price.
    OPTION_MANAGED: bool = _bool("OPTION_MANAGED", True)
    # Evaluate open positions on every streamed tick (Kite WebSocket).
    TICK_MANAGEMENT: bool = _bool("TICK_MANAGEMENT", True)
    # Persist the mark-to-market of an open position at most this often (s).
    TICK_PERSIST_SEC: int = _int("TICK_PERSIST_SEC", 5)
    # An open option price older than this is shown as stale on the dashboard.
    OPTION_QUOTE_STALE_SEC: int = _int("OPTION_QUOTE_STALE_SEC", 120)

    @property
    def is_positional(self) -> bool:
        return self.TRADING_MODE == "positional"
    # With closed-bar scanning the entry is the live price, not the bar close.
    # If price has already run more than this many ATRs past the closed bar's
    # close, the breakout is being chased and the setup is skipped.
    MAX_ENTRY_SLIP_ATR: float = _float("MAX_ENTRY_SLIP_ATR", 1.0)

    # ---------- stop placement ----------
    # The stop is a MARKET level, never a rupee amount. It sits at the widest
    # of four distances from entry, so a squeeze-compressed ATR can no longer
    # put the stop inside the noise the expansion is about to create:
    #   ATR_SL_MULT x ATR(5m)              the original rule
    #   SL_RANGE_FRACTION x squeeze range  half the coil
    #   SL_HTF_ATR_MULT x ATR(15m)         the higher timeframe is not compressed
    #   MIN_SL_PCT x price                 an absolute floor in % of price
    # MAX_SL_DISTANCE_PCT stays as the ceiling: wider than that, no trade.
    ATR_LENGTH: int = _int("ATR_LENGTH", 14)
    ATR_SL_MULT: float = _float("ATR_SL_MULT", 2.0)
    SL_RANGE_FRACTION: float = _float("SL_RANGE_FRACTION", 0.5)
    SL_HTF_ATR_MULT: float = _float("SL_HTF_ATR_MULT", 1.0)
    MIN_SL_PCT: float = _float("MIN_SL_PCT", 0.25)          # indices
    EQ_MIN_SL_PCT: float = _float("EQ_MIN_SL_PCT", 0.6)     # stocks
    MAX_SL_DISTANCE_PCT: float = _float("MAX_SL_DISTANCE_PCT", 2.0)

    # ---------- targets ----------
    # "risk": targets are multiples of the stop distance (R). TP1 at 1.5R
    #         means the first partial pays more than a full stop costs.
    # "atr":  the previous behaviour - targets at ATR multiples regardless of
    #         where the stop is, which produced 0.5R first targets.
    TARGET_MODE: str = os.getenv("TARGET_MODE", "risk").strip().lower()
    TARGET_R_MULTS: List[float] = field(default_factory=lambda: _floats("TARGET_R_MULTS", [1.5, 2.5, 4.0]))
    ATR_TP1_MULT: float = _float("ATR_TP1_MULT", 1.0)
    ATR_TP2_MULT: float = _float("ATR_TP2_MULT", 2.0)
    ATR_TP3_MULT: float = _float("ATR_TP3_MULT", 3.0)
    # Scale-out fractions at TP1 / TP2 / runner. Must sum to 1.0.
    # Override with SCALE_OUT=0.5,0.3,0.2
    SCALE_OUT: List[float] = field(default_factory=lambda: _floats("SCALE_OUT", [0.5, 0.3, 0.2]))

    # ---------- position sizing ----------
    # "notional": lots = CAPITAL_PER_TRADE / margin for one lot, where margin
    #             is MARGIN_PCT of contract value. The rupee loss on a stop
    #             then follows the market distance, not the other way round.
    # "risk":     the previous behaviour - lots chosen so a stop-out costs
    #             RISK_PER_TRADE_PCT of ACCOUNT_BALANCE.
    SIZING_MODE: str = os.getenv("SIZING_MODE", "notional").strip().lower()
    CAPITAL_PER_TRADE: float = _float("CAPITAL_PER_TRADE", 250_000)
    # Approximate intraday futures margin as % of contract value. Index ~12%,
    # stock futures 15-25% depending on the scrip. Verify against your broker.
    MARGIN_PCT: float = _float("MARGIN_PCT", 12.0)
    EQ_MARGIN_PCT: float = _float("EQ_MARGIN_PCT", 20.0)
    MAX_LOTS_PER_TRADE: int = _int("MAX_LOTS_PER_TRADE", 0)    # 0 = no cap
    # Used by SIZING_MODE=risk, and for the informational risk % on every
    # signal in either mode.
    ACCOUNT_BALANCE: float = _float("ACCOUNT_BALANCE", 1_000_000)
    RISK_PER_TRADE_PCT: float = _float("RISK_PER_TRADE_PCT", 1.0)
    MAX_RISK_PER_TRADE_PCT: float = _float("MAX_RISK_PER_TRADE_PCT", 2.0)
    # Index-futures costs: exchange txn charges + GST + stamp are roughly
    # 0.005% of turnover per side, plus flat brokerage per leg. Options on
    # premium cost far more - raise these if you track option P&L.
    COST_PCT_PER_SIDE: float = _float("COST_PCT_PER_SIDE", 0.00005)
    BROKERAGE_PER_LEG: float = _float("BROKERAGE_PER_LEG", 20.0)

    # ---------- exits ----------
    USE_SUPERTREND_EXIT: bool = _bool("USE_SUPERTREND_EXIT", True)
    ST_PERIOD: int = _int("ST_PERIOD", 10)
    ST_MULT: float = _float("ST_MULT", 3.0)
    MOVE_SL_TO_BE_AFTER_TP1: bool = _bool("MOVE_SL_TO_BE_AFTER_TP1", True)

    # ---------- options overlay ----------
    OPTIONS_ENABLED: bool = _bool("OPTIONS_ENABLED", True)
    # When an option gate blocks, does the whole signal die, or does it stand
    # as a futures-level signal with the option leg flagged?
    OPTIONS_BLOCK_SIGNAL: bool = _bool("OPTIONS_BLOCK_SIGNAL", False)
    OPTIONS_BLOCK_ON_MISSING_DATA: bool = _bool("OPTIONS_BLOCK_ON_MISSING_DATA", False)
    # IV rank band. The upper bound is the one that protects an option buyer:
    # entering rich vol means a crush can lose money on a correct call. The
    # lower bound skips regimes where the market prices no movement at all.
    MIN_IV_RANK: float = _float("MIN_IV_RANK", 20.0)
    MAX_IV_RANK: float = _float("MAX_IV_RANK", 80.0)
    IV_RANK_LOOKBACK_DAYS: int = _int("IV_RANK_LOOKBACK_DAYS", 90)
    # ---------- risk limits ----------
    # No new signals once today's result (closed R + open trades marked to
    # market) is at or below -DAILY_LOSS_LIMIT_R, or this week's is at or
    # below -WEEKLY_LOSS_LIMIT_R. 0 turns a limit off.
    DAILY_LOSS_LIMIT_R: float = _float("DAILY_LOSS_LIMIT_R", 2.0)
    WEEKLY_LOSS_LIMIT_R: float = _float("WEEKLY_LOSS_LIMIT_R", 5.0)
    # At most this many open positions in one NSE industry and direction
    # (sectors.py). 0 = off.
    SECTOR_MAX_OPEN: int = _int("SECTOR_MAX_OPEN", 2)
    SECTOR_SOURCE_URL: str = os.getenv(
        "SECTOR_SOURCE_URL", "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv")
    # ---------- data retention (retention.py, daily after the close) ----------
    RETENTION_SCAN_DAYS: int = _int("RETENTION_SCAN_DAYS", 30)
    RETENTION_CANDLE_DAYS: int = _int("RETENTION_CANDLE_DAYS", 90)
    RETENTION_SIGNAL_DAYS: int = _int("RETENTION_SIGNAL_DAYS", 400)
    IV_RANK_MIN_SAMPLES: int = _int("IV_RANK_MIN_SAMPLES", 60)
    IV_RANK_MIN_DAYS: int = _int("IV_RANK_MIN_DAYS", 10)
    # Expiry-day theta gate: no new option entries after this IST time on the
    # day the contract expires.
    THETA_CUTOFF_TIME: str = os.getenv("THETA_CUTOFF_TIME", "13:30")
    EXPIRY_DAY_ROLLOVER: bool = _bool("EXPIRY_DAY_ROLLOVER", True)
    # A flat "%% of premium per day" cap is the wrong yardstick for an intraday
    # system: a weekly ATM option burns ~7%%/day at 7 DTE and ~30%%/day at 2 DTE,
    # but a position held 90 minutes only pays a fraction of that. So the real
    # test is theta over the expected hold versus the move the signal expects.
    # Off by default (0 disables it). A "% of premium per day" ceiling is a
    # swing trader's metric: this scanner squares off at 15:20, so a contract
    # losing 72%/day only pays ~2 hours of that. The drag test below measures
    # the cost you actually bear. Expiry-day risk is handled by THETA_CUTOFF.
    MAX_THETA_PCT_PER_DAY: float = _float("MAX_THETA_PCT_PER_DAY", 0.0)
    EXPECTED_HOLD_HOURS: float = _float("EXPECTED_HOLD_HOURS", 2.0)
    MAX_THETA_DRAG_PCT: float = _float("MAX_THETA_DRAG_PCT", 33.0)
    # Option liquidity. Measured against the expected move, not against premium
    # - the same yardstick used for theta, so the two are comparable. Requires a
    # feed that publishes market depth, which Kite does.
    MAX_SPREAD_DRAG_PCT: float = _float("MAX_SPREAD_DRAG_PCT", 25.0)
    MIN_OPTION_OI: float = _float("MIN_OPTION_OI", 50_000)
    RISK_FREE_RATE: float = _float("RISK_FREE_RATE", 0.065)
    IV_SAMPLE_MINUTES: int = _int("IV_SAMPLE_MINUTES", 15)

    # ---------- housekeeping ----------
    COOLDOWN_BARS: int = _int("COOLDOWN_BARS", 12)
    MAX_OPEN_PER_SYMBOL: int = _int("MAX_OPEN_PER_SYMBOL", 1)
    MAX_SIGNALS_PER_DAY: int = _int("MAX_SIGNALS_PER_DAY", 5)
    MAX_CONCURRENT_FETCHES: int = _int("MAX_CONCURRENT_FETCHES", 3)
    # Calls per second allowed to the broker. Concurrency and rate are different
    # limits - see RateLimiter in feed_base.py.
    API_CALLS_PER_SECOND: float = _float("API_CALLS_PER_SECOND", 3.0)
    USE_CANDLE_STORE: bool = _bool("USE_CANDLE_STORE", True)
    # How long a failed quote is allowed to fall back to the last cached one
    # before that cache is refused instead of silently served as if fresh.
    # Without a ceiling here, a persistent quote failure (not just a one-off
    # blip) keeps replaying the same old price under a new timestamp forever -
    # the dashboard looks live but has quietly stopped tracking the market.
    QUOTE_CACHE_MAX_AGE_SECONDS: int = _int("QUOTE_CACHE_MAX_AGE_SECONDS", 90)
    # Derive the higher timeframe from stored bars instead of a second request.
    DERIVE_HTF: bool = _bool("DERIVE_HTF", True)
    CANDLE_RETENTION_DAYS: int = _int("CANDLE_RETENTION_DAYS", 30)
    SCAN_ONLY_MARKET_HOURS: bool = _bool("SCAN_ONLY_MARKET_HOURS", True)

    # ---------- dynamic equity universe ----------
    # Indices are tracked separately from stocks; these govern the stock side.
    EQUITY_ENABLED: bool = _bool("EQUITY_ENABLED", True)
    UNIVERSE_SIZE: int = _int("UNIVERSE_SIZE", 200)
    # Drop cheap scrips: a 2% ATR move on a Rs 80 stock is noise, and the
    # option strikes are too coarse relative to premium.
    MIN_STOCK_PRICE: float = _float("MIN_STOCK_PRICE", 200.0)
    MIN_TURNOVER_CR: float = _float("MIN_TURNOVER_CR", 5.0)      # Rs crore/day
    # Ranking blend. Turnover buys you fills; momentum buys you movement.
    RANK_W_TURNOVER: float = _float("RANK_W_TURNOVER", 0.6)
    RANK_W_MOMENTUM: float = _float("RANK_W_MOMENTUM", 0.4)
    MOMENTUM_DAYS: int = _int("MOMENTUM_DAYS", 5)
    QUOTE_BATCH_SIZE: int = _int("QUOTE_BATCH_SIZE", 50)

    # ---------- index isolation ----------
    # Stocks gap, trend and reverse differently from indices, so the equity
    # path gets its own thresholds rather than inheriting index tuning.
    EQ_VOLUME_MULT: float = _float("EQ_VOLUME_MULT", 2.0)
    EQ_ADX_THRESHOLD: float = _float("EQ_ADX_THRESHOLD", 25.0)
    EQ_MIN_COMPOSITE: float = _float("EQ_MIN_COMPOSITE", 0.75)
    EQ_ATR_SL_MULT: float = _float("EQ_ATR_SL_MULT", 2.5)
    EQ_MAX_SL_DISTANCE_PCT: float = _float("EQ_MAX_SL_DISTANCE_PCT", 3.0)

    # ---------- websocket streaming ----------
    USE_WEBSOCKET: bool = _bool("USE_WEBSOCKET", True)
    WS_RECONNECT_SECONDS: int = _int("WS_RECONNECT_SECONDS", 15)
    WS_STALE_SECONDS: int = _int("WS_STALE_SECONDS", 120)
    # Unsubscribe the feed once the daily cap is reached.
    WS_UNSUBSCRIBE_AT_CAP: bool = _bool("WS_UNSUBSCRIBE_AT_CAP", True)

    # ---------- infra ----------
    # "kite" | "mock". Nothing else is valid - build_feed() raises otherwise.
    FEED_MODE: str = os.getenv("FEED_MODE", "kite").strip().lower()
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    PORT: int = _int("PORT", 8000)
    # Set false on the web service when a separate worker owns the jobs.
    RUN_SCHEDULER: bool = _bool("RUN_SCHEDULER", True)
    # Worker only. When true, the worker exits on its own at WORKER_STOP_TIME
    # (IST), and exits immediately if started on a weekend, a MARKET_HOLIDAYS
    # date, or after the stop time. Pair it with a Railway cron schedule that
    # starts the worker each weekday morning, so the service holds memory only
    # during market hours. Off by default: without a cron schedule to start it
    # again, an exiting worker would simply stay down.
    WORKER_AUTO_STOP: bool = _bool("WORKER_AUTO_STOP", False)
    WORKER_STOP_TIME: str = os.getenv("WORKER_STOP_TIME", "18:00")
    DASHBOARD_TOKEN: str = os.getenv("DASHBOARD_TOKEN", "")             # blank = no auth
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


    # ---------- Kite Connect ----------
    KITE_API_KEY: str = os.getenv("KITE_API_KEY", "")
    KITE_API_SECRET: str = os.getenv("KITE_API_SECRET", "")
    # Optional. Normally the token is issued through /kite/login and stored in
    # the database, because Kite tokens expire every morning and can only be
    # minted via a browser redirect.
    KITE_ACCESS_TOKEN: str = os.getenv("KITE_ACCESS_TOKEN", "")
    # Public base URL, used to build the redirect shown on the login page.
    PUBLIC_URL: str = os.getenv("PUBLIC_URL", os.getenv("RAILWAY_PUBLIC_DOMAIN", ""))

    # ---------- Kite auto-login (TOTP) ----------
    # Enables the scheduled job_auto_login job in main.py. Off by default -
    # scripted login is against Kite's ToS (see kite_autologin.py) and must be
    # opted into explicitly.
    KITE_AUTO_LOGIN: bool = _bool("KITE_AUTO_LOGIN", False)
    KITE_AUTO_LOGIN_HOUR: int = _int("KITE_AUTO_LOGIN_HOUR", 8)
    KITE_AUTO_LOGIN_MINUTE: int = _int("KITE_AUTO_LOGIN_MINUTE", 0)
    KITE_USER_ID: str = os.getenv("KITE_USER_ID", "")
    KITE_PASSWORD: str = os.getenv("KITE_PASSWORD", "")
    KITE_TOTP_SECRET: str = os.getenv("KITE_TOTP_SECRET", "")

    # ---------- notifications ----------
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: int = int(os.getenv("TELEGRAM_CHAT_ID", "0") or "0")

    # Once-a-day Telegram alert with the NIFTY 50 / BANKNIFTY / SENSEX opening
    # status, sent the first time the scanner is live after the market opens.
    MARKET_OPEN_ALERT: bool = _bool("MARKET_OPEN_ALERT", True)
    MARKET_OPEN_ALERT_HOUR: int = _int("MARKET_OPEN_ALERT_HOUR", 9)
    MARKET_OPEN_ALERT_MINUTE: int = _int("MARKET_OPEN_ALERT_MINUTE", 16)

    def __post_init__(self):
        if self.TIMEFRAME not in ("5m", "15m"):
            self.TIMEFRAME = "5m"
        self.BAR_MINUTES = 5 if self.TIMEFRAME == "5m" else 15
        if self.SQUEEZE_TIMEFRAME not in ("5m", "15m"):
            self.SQUEEZE_TIMEFRAME = "5m"
        if self.TARGET_MODE not in ("risk", "atr"):
            self.TARGET_MODE = "risk"
        if self.SIZING_MODE not in ("notional", "risk"):
            self.SIZING_MODE = "notional"
        # Three ascending target multiples, or the default.
        if len(self.TARGET_R_MULTS) != 3 or any(m <= 0 for m in self.TARGET_R_MULTS) \
                or self.TARGET_R_MULTS != sorted(self.TARGET_R_MULTS):
            self.TARGET_R_MULTS = [1.5, 2.5, 4.0]
        # Three fractions that add to one, or the default.
        if len(self.SCALE_OUT) != 3 or any(f < 0 for f in self.SCALE_OUT) \
                or abs(sum(self.SCALE_OUT) - 1.0) > 0.01:
            self.SCALE_OUT = [0.5, 0.3, 0.2]

    def min_sl_pct(self, symbol: str) -> float:
        return self.MIN_SL_PCT if self.is_index(symbol) else self.EQ_MIN_SL_PCT

    def margin_pct(self, symbol: str) -> float:
        return self.MARGIN_PCT if self.is_index(symbol) else self.EQ_MARGIN_PCT

    def set_timeframe(self, tf: str) -> None:
        if tf in ("5m", "15m"):
            self.TIMEFRAME = tf
            self.BAR_MINUTES = 5 if tf == "5m" else 15

    def lot_size(self, symbol: str) -> int:
        return int(self.LOT_SIZES.get(symbol, self.DEFAULT_LOT_SIZE))

    def strike_step(self, symbol: str) -> int:
        return int(self.STRIKE_STEPS.get(symbol, 50))

    def is_index(self, symbol: str) -> bool:
        return symbol in self.INDICES

    def volume_mult(self, symbol: str) -> float:
        return self.VOLUME_MULT if self.is_index(symbol) else self.EQ_VOLUME_MULT

    def adx_threshold(self, symbol: str) -> float:
        return self.ADX_THRESHOLD if self.is_index(symbol) else self.EQ_ADX_THRESHOLD

    def min_composite(self, symbol: str) -> float:
        return self.MIN_COMPOSITE if self.is_index(symbol) else self.EQ_MIN_COMPOSITE

    def sl_mult(self, symbol: str) -> float:
        return self.ATR_SL_MULT if self.is_index(symbol) else self.EQ_ATR_SL_MULT

    def max_sl_pct(self, symbol: str) -> float:
        return self.MAX_SL_DISTANCE_PCT if self.is_index(symbol) else self.EQ_MAX_SL_DISTANCE_PCT

    def missing_credentials(self) -> List[str]:
        """
        Every missing variable, in one list.

        Reporting them one at a time means a redeploy per variable, and on
        Railway that is a slow way to find out you also forgot one.
        """
        if self.FEED_MODE == "mock":
            return []

        return [name for name, value in (
            ("KITE_API_KEY", self.KITE_API_KEY),
            ("KITE_API_SECRET", self.KITE_API_SECRET),
        ) if not value]

    def credentials_present(self) -> bool:
        return not self.missing_credentials()


cfg = StrategyConfig()
