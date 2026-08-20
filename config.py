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

    # ---------- risk ----------
    ACCOUNT_BALANCE: float = _float("ACCOUNT_BALANCE", 1_000_000)
    RISK_PER_TRADE_PCT: float = _float("RISK_PER_TRADE_PCT", 1.0)
    MAX_RISK_PER_TRADE_PCT: float = _float("MAX_RISK_PER_TRADE_PCT", 2.0)
    ATR_LENGTH: int = _int("ATR_LENGTH", 14)
    ATR_SL_MULT: float = _float("ATR_SL_MULT", 1.5)
    ATR_TP1_MULT: float = _float("ATR_TP1_MULT", 1.0)
    ATR_TP2_MULT: float = _float("ATR_TP2_MULT", 2.0)
    ATR_TP3_MULT: float = _float("ATR_TP3_MULT", 3.0)
    MAX_SL_DISTANCE_PCT: float = _float("MAX_SL_DISTANCE_PCT", 2.0)
    # Scale-out fractions at TP1 / TP2 / runner. Must sum to 1.0.
    SCALE_OUT: List[float] = field(default_factory=lambda: [0.33, 0.33, 0.34])
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
    EQ_ATR_SL_MULT: float = _float("EQ_ATR_SL_MULT", 2.0)
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
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    def __post_init__(self):
        if self.TIMEFRAME not in ("5m", "15m"):
            self.TIMEFRAME = "5m"
        self.BAR_MINUTES = 5 if self.TIMEFRAME == "5m" else 15

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
