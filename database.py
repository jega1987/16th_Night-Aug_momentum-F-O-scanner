"""
Persistence layer.

Uses DATABASE_URL when present (Railway Postgres) and falls back to a local
SQLite file otherwise. Railway's filesystem is ephemeral, so SQLite there means
losing every signal on redeploy - attach the Postgres plugin for anything you
care about keeping.
"""
import logging
import os
from contextlib import contextmanager

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, Index, Integer,
                        String, UniqueConstraint, create_engine, text)
from sqlalchemy.orm import declarative_base, sessionmaker

from clock import now_naive
from config import cfg

logger = logging.getLogger(__name__)
Base = declarative_base()


def _build_engine():
    """
    Build the SQLAlchemy engine.

    This used to call create_engine() directly at import time with no
    validation. A malformed DATABASE_URL crashed on every single startup
    attempt, and the traceback never showed what the bad value actually was -
    just "Could not parse SQLAlchemy URL from given URL string", from deep
    inside SQLAlchemy's own parser. That is not enough to fix the problem from
    the log alone. Every check below exists to either name the mistake or
    refuse to let a bad database setting take the whole app down.
    """
    raw = (cfg.DATABASE_URL or "").strip()
    # A common paste artifact: the value copied with its surrounding quotes.
    if raw and raw[0] in "\"'" and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()

    if raw and ("${{" in raw or "}}" in raw):
        logger.error(
            "[DB] DATABASE_URL is the literal text '%s' - Railway's ${{...}} "
            "reference was never resolved. This happens when the syntax is typed "
            "by hand instead of chosen from Railway's autocomplete dropdown, or "
            "when the referenced service is not named exactly 'Postgres'. Fix: open "
            "the Postgres service -> Variables -> copy the DATABASE_URL value "
            "directly (starts with postgresql://) and paste that literal value into "
            "this service's DATABASE_URL instead of the ${{...}} reference. "
            "Falling back to SQLite so the app can start in the meantime.",
            raw[:80])
        raw = ""

    elif raw and not raw.startswith(("postgres://", "postgresql://", "postgresql+psycopg2://")):
        logger.error(
            "[DB] DATABASE_URL does not look like a Postgres URL (starts with %r, "
            "%d chars). Expected it to begin with postgresql://. Falling back to "
            "SQLite so the app can start - fix the variable to use Postgres.",
            raw[:20], len(raw))
        raw = ""

    if raw:
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql+psycopg2://", 1)
        elif raw.startswith("postgresql://") and not raw.startswith("postgresql+psycopg2://"):
            raw = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
        try:
            engine = create_engine(raw, pool_pre_ping=True, pool_recycle=280, future=True)
            with engine.connect():
                pass
            logger.info("[DB] Using Postgres")
            return engine
        except Exception as exc:
            # Never let a bad connection string take the whole process down.
            # A crash loop over a typo in one variable is a worse outcome than
            # starting on SQLite and saying so clearly.
            logger.error("[DB] Could not connect to Postgres (%s) - "
                         "falling back to SQLite so the app can start", exc)

    os.makedirs("./data", exist_ok=True)
    logger.warning("[DB] No usable DATABASE_URL - falling back to SQLite at "
                   "./data/scanner.db (data is lost on every Railway redeploy)")
    return create_engine(
        "sqlite:///./data/scanner.db",
        connect_args={"check_same_thread": False},
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    timeframe = Column(String, index=True)
    direction = Column(String)
    asset_class = Column(String, default="INDEX", index=True)   # INDEX | EQUITY

    entry = Column(Float)
    sl = Column(Float)              # original stop
    trail_sl = Column(Float)        # live stop after breakeven / supertrend trail
    tp1 = Column(Float)
    tp2 = Column(Float)
    tp3 = Column(Float)
    qty = Column(Integer)
    lots = Column(Integer)
    atr14 = Column(Float)
    atm_strike = Column(Integer)
    option_hint = Column(String)    # e.g. "NIFTY 24500 CE"

    # --- options overlay ---
    option_type = Column(String)            # CE | PE
    option_expiry = Column(String)
    option_dte = Column(Integer)
    option_ltp = Column(Float)
    option_iv = Column(Float)               # decimal, 0.14 == 14%
    option_iv_rank = Column(Float)          # 0-100, null until history exists
    option_delta = Column(Float)
    option_theta_pct = Column(Float)        # premium burned per day, %
    option_blocked = Column(Boolean, default=False)
    option_block_reason = Column(String)

    score_direction = Column(Float)
    score_squeeze = Column(Float)
    score_sweep = Column(Float)
    score_structure = Column(Float)
    score_volume = Column(Float)
    score_rsi = Column(Float)
    score_oi = Column(Float)
    score_adx = Column(Float)
    score_htf = Column(Float)
    composite_score = Column(Float)
    factor_breakdown = Column(JSON)

    status = Column(String, default="OPEN", index=True)   # OPEN | RUNNING | TP3 | SL | TRAIL | SQUAREOFF
    tp1_hit = Column(Boolean, default=False)
    tp2_hit = Column(Boolean, default=False)
    qty_open = Column(Integer)
    realized_pnl = Column(Float, default=0.0)
    exit_price = Column(Float)
    exit_time = Column(DateTime)
    pnl = Column(Float, default=0.0)
    pnl_pct = Column(Float)
    r_multiple = Column(Float)
    mfe = Column(Float)             # best price seen while open
    triggered_by = Column(String, default="scanner")
    notes = Column(String)


class IndexSnapshot(Base):
    __tablename__ = "index_snapshots"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    ltp = Column(Float)
    open_price = Column(Float)
    high = Column(Float)
    low = Column(Float)
    prev_close = Column(Float)
    change_pct = Column(Float)
    change_abs = Column(Float)
    volume = Column(Integer)
    oi = Column(Integer)


class OISnapshot(Base):
    """Open interest history - needed to compute a real OI change instead of
    guessing. One row per symbol per scan."""
    __tablename__ = "oi_snapshots"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    oi = Column(Float)
    price = Column(Float)


class Candle(Base):
    """
    Stored OHLCV. A closed bar never changes, so it is fetched once and reused
    for the life of the deployment.
    """
    __tablename__ = "candles"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    timeframe = Column(String, index=True)
    timestamp = Column(DateTime, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle"),
        Index("ix_candle_lookup", "symbol", "timeframe", "timestamp"),
    )


class BrokerToken(Base):
    """
    Daily broker session token.

    Kite access tokens expire each morning and are only issued through a
    browser redirect, which does not fit an always-on container. Persisting the
    token means the morning login is one click rather than a redeploy, and it
    survives restarts.
    """
    __tablename__ = "broker_tokens"
    id = Column(Integer, primary_key=True)
    broker = Column(String, index=True)
    access_token = Column(String)
    public_token = Column(String)
    user_id = Column(String)
    issued_at = Column(DateTime, default=now_naive, index=True)


class UniverseMember(Base):
    """One row per stock selected into today's tradable universe."""
    __tablename__ = "universe_members"
    id = Column(Integer, primary_key=True)
    selected_on = Column(DateTime, default=now_naive, index=True)
    rank = Column(Integer, index=True)
    symbol = Column(String, index=True)
    root = Column(String)
    scrip_code = Column(Integer)
    exch = Column(String)
    exch_type = Column(String)
    lot_size = Column(Integer, default=0)
    expiry = Column(String, nullable=True)
    ltp = Column(Float)
    turnover_cr = Column(Float)
    momentum_pct = Column(Float)
    momentum_source = Column(String)     # "5d" | "1d" | "none" - never guessed
    rank_score = Column(Float)


class IVSnapshot(Base):
    """ATM implied volatility history. Without this, IV rank is a guess."""
    __tablename__ = "iv_snapshots"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    atm_iv = Column(Float)
    futures_price = Column(Float)
    expiry = Column(String)


class ScanLog(Base):
    __tablename__ = "scan_logs"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    timeframe = Column(String, index=True)
    in_squeeze = Column(Boolean)
    squeeze_bars = Column(Integer)
    bars_since_fire = Column(Integer)
    fired = Column(Boolean)
    close = Column(Float)
    adx_value = Column(Float)
    rsi_value = Column(Float)
    vol_ratio = Column(Float)
    oi_change_pct = Column(Float)
    composite_score = Column(Float)
    passed = Column(Boolean)
    rejection_reason = Column(String)
    # Per-factor scores (direction, squeeze, volume, adx, rsi, structure,
    # sweep, oi, htf, composite, composite_all) - the same dict filters.py
    # computed, so the dashboard/API can show which factor failed instead of
    # just the composite number.
    factors = Column(JSON)


class ScripMapping(Base):
    __tablename__ = "scrip_mappings"
    id = Column(Integer, primary_key=True)
    updated_at = Column(DateTime, default=now_naive, index=True)
    symbol = Column(String, index=True)
    root_name = Column(String)
    scrip_code = Column(Integer)
    exch = Column(String)
    exch_type = Column(String)
    contract_name = Column(String)
    expiry_date = Column(String, nullable=True)
    lot_size = Column(Integer, default=0)
    is_current = Column(Boolean, default=True, index=True)
    oi = Column(Integer, default=0)


def _add_missing_columns() -> None:
    """
    create_all() creates tables but never ALTERs existing ones. On Railway you
    can't casually drop the database, so add any new columns in place.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)
    dialect = engine.dialect.name

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            if dialect == "postgresql" and col_type.upper() == "JSON":
                col_type = "JSONB"
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'))
                logger.info("[DB] Added %s.%s", table.name, column.name)
            except Exception as exc:
                logger.warning("[DB] Could not add %s.%s: %s", table.name, column.name, exc)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("[DB] Schema ready")


@contextmanager
def session_scope():
    """Commit on success, roll back on failure, always close."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
