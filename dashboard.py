"""
Web layer: one HTML page plus a small JSON API.

The dashboard is read-only apart from /api/scan-now and the timeframe toggle.
Set DASHBOARD_TOKEN to keep a public Railway URL private.
"""
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import analytics
from clock import MarketClock, now_naive, today_start
from config import cfg
from database import IndexSnapshot, ScanLog, Signal, get_db
from runtime import budget, engine, feed, scanner, state, store, universe

logger = logging.getLogger(__name__)

app = FastAPI(title="Index Squeeze Scanner", docs_url="/docs", redoc_url=None)
templates = Jinja2Templates(directory="templates")

OPEN_STATES = ("OPEN", "RUNNING")
CLOSED_STATES = ("TP3", "SL", "TRAIL", "SQUAREOFF")


def require_token(request: Request, token: Optional[str] = Query(default=None)):
    """No-op when DASHBOARD_TOKEN is unset. Otherwise accepts ?token=,
    an X-Dashboard-Token header, or a cookie set on first successful visit."""
    if not cfg.DASHBOARD_TOKEN:
        return True
    supplied = token or request.headers.get("x-dashboard-token") or request.cookies.get("dash_token")
    if supplied != cfg.DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Add ?token=... to the URL to view this dashboard")
    return True


# --------------------------------------------------------------------------- #
@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """Railway health check. Reports degraded rather than failing so a broker
    outage doesn't put the service into a restart loop."""
    try:
        signals_today = db.query(Signal).filter(Signal.timestamp >= today_start()).count()
        db_ok = True
    except Exception as exc:
        signals_today, db_ok = 0, False
        logger.error("[Health] DB check failed: %s", exc)

    return JSONResponse({
        "status": "ok" if db_ok else "degraded",
        "feed": {"mode": cfg.FEED_MODE, "connected": state.feed_connected,
                 "error": state.feed_error},
        "config": {"ok": not state.config_error,
                   "error": state.config_error,
                   "missing": cfg.missing_credentials(),
                   "database": "postgres" if cfg.DATABASE_URL else "sqlite (ephemeral)"},
        "market": MarketClock.session_label(),
        "timeframe": cfg.TIMEFRAME,
        "scans": state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() if state.last_scan_at else None,
        "last_scan_error": state.last_scan_error,
        "signals_today": signals_today,
        "candle_store": store.stats(),
        "budget": budget.check(),
        "universe": {"size": state.universe_size, "symbols": len(universe.load())},
        "stream": getattr(getattr(feed, "stream", None), "status", lambda: None)(),
        "server_time_ist": now_naive().isoformat(timespec="seconds"),
    })


@app.get("/")
async def dashboard(request: Request,
                    tf: str = Query(default=None),
                    token: Optional[str] = Query(default=None),
                    db: Session = Depends(get_db),
                    _=Depends(require_token)):
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME

    snapshots = {}
    for symbol in cfg.INDICES:
        snap = (db.query(IndexSnapshot)
                  .filter(IndexSnapshot.symbol == symbol)
                  .order_by(IndexSnapshot.timestamp.desc())
                  .first())
        if snap:
            snapshots[symbol] = snap

    squeeze_state = {}
    for symbol in cfg.INDICES:
        row = (db.query(ScanLog)
                 .filter(ScanLog.symbol == symbol, ScanLog.timeframe == tf)
                 .order_by(ScanLog.timestamp.desc())
                 .first())
        if row:
            squeeze_state[symbol] = row

    today_signals = (db.query(Signal)
                       .filter(Signal.timeframe == tf, Signal.timestamp >= today_start())
                       .all())
    closed = [s for s in today_signals if s.status in CLOSED_STATES]
    wins = [s for s in closed if (s.pnl or 0) > 0]

    recent = (db.query(Signal)
                .filter(Signal.timeframe == tf)
                .order_by(Signal.timestamp.desc())
                .limit(15).all())
    logs = (db.query(ScanLog)
              .filter(ScanLog.timeframe == tf)
              .order_by(ScanLog.timestamp.desc())
              .limit(40).all())

    ctx = {
        "timeframe": tf,
        "indices": cfg.INDICES,
        "snapshots": snapshots,
        "squeeze_state": squeeze_state,
        "min_squeeze_bars": cfg.MIN_SQUEEZE_BARS,
        "session": MarketClock.session_label(),
        "feed_mode": cfg.FEED_MODE,
        "feed_connected": state.feed_connected,
        "config_error": state.config_error,
        "missing_vars": cfg.missing_credentials(),
        "last_scan": state.last_scan_at.strftime("%H:%M:%S") if state.last_scan_at else "not yet",
        "budget": budget.check(),
        "watchlist_size": len(scanner.symbols()),
        "total_signals": len(today_signals),
        "open_signals": len([s for s in today_signals if s.status in OPEN_STATES]),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(sum(s.pnl or 0 for s in closed), 2),
        "recent_signals": recent,
        "scan_logs": logs,
        "now": now_naive().strftime("%d %b %Y, %H:%M IST"),
        "token": token or "",
    }
    page = templates.TemplateResponse(request, "index.html", ctx)
    if cfg.DASHBOARD_TOKEN and token == cfg.DASHBOARD_TOKEN:
        page.set_cookie("dash_token", token, httponly=True, samesite="lax", max_age=86400)
    return page


# --------------------------------------------------------------------------- #
@app.get("/api/signals")
async def api_signals(tf: str = Query(default=None), limit: int = Query(default=50, le=200),
                      db: Session = Depends(get_db), _=Depends(require_token)):
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME
    rows = (db.query(Signal).filter(Signal.timeframe == tf)
              .order_by(Signal.timestamp.desc()).limit(limit).all())
    return [{
        "id": r.id, "time": r.timestamp.strftime("%d-%m %H:%M") if r.timestamp else None,
        "symbol": r.symbol, "direction": r.direction, "entry": r.entry,
        "sl": r.sl, "trail_sl": r.trail_sl, "tp1": r.tp1, "tp2": r.tp2, "tp3": r.tp3,
        "qty": r.qty, "lots": r.lots, "status": r.status, "pnl": r.pnl,
        "r_multiple": r.r_multiple, "score": r.composite_score,
        "option_hint": r.option_hint, "factors": r.factor_breakdown,
    } for r in rows]


@app.get("/api/equity")
async def api_equity(tf: str = Query(default=None), db: Session = Depends(get_db),
                     _=Depends(require_token)):
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME
    rows = (db.query(Signal)
              .filter(Signal.timeframe == tf, Signal.status.in_(CLOSED_STATES))
              .order_by(Signal.timestamp).all())
    curve, running = [], 0.0
    for r in rows:
        running += r.pnl or 0
        curve.append({"time": r.timestamp.strftime("%d-%m %H:%M"), "pnl": round(running, 2)})
    return curve


@app.get("/api/stats")
async def api_stats(tf: str = Query(default=None), _=Depends(require_token)):
    return engine.daily_stats(tf if tf in ("5m", "15m") else cfg.TIMEFRAME)


@app.get("/api/scan-now")
async def api_scan_now(force: bool = Query(default=True), _=Depends(require_token)):
    """Run a scan immediately, ignoring the market-hours gate by default."""
    try:
        created = await scanner.run_cycle(force=force)
        state.last_scan_at = now_naive()
        state.scan_count += 1
        return {"ok": True, "signals_created": len(created),
                "signals": [{"id": c["id"], "symbol": c["symbol"], "direction": c["direction"],
                             "entry": c["entry"], "score": c["composite_score"]} for c in created]}
    except Exception as exc:
        logger.exception("[API] Manual scan failed")
        state.last_scan_error = str(exc)
        raise HTTPException(status_code=502, detail=f"Scan failed: {exc}")


@app.get("/api/switch-timeframe")
async def api_switch_timeframe(tf: str = Query(...), _=Depends(require_token)):
    if tf not in ("5m", "15m"):
        raise HTTPException(status_code=400, detail="Timeframe must be 5m or 15m")
    cfg.set_timeframe(tf)
    return {"ok": True, "timeframe": cfg.TIMEFRAME, "bar_minutes": cfg.BAR_MINUTES}


@app.get("/api/square-off")
async def api_square_off(_=Depends(require_token)):
    closed = await engine.force_square_off(feed, reason="Manual square-off")
    return {"ok": True, "closed": len(closed)}


@app.get("/performance")
async def performance_page(request: Request,
                           days: int = Query(default=30, ge=1, le=730),
                           tf: str = Query(default=None),
                           token: Optional[str] = Query(default=None),
                           _=Depends(require_token)):
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME
    report = analytics.full_report(days, tf)
    ctx = {
        "timeframe": tf, "days": days, "token": token or "",
        "now": now_naive().strftime("%d %b %Y, %H:%M IST"),
        **report,
    }
    return templates.TemplateResponse(request, "performance.html", ctx)


@app.get("/api/performance")
async def api_performance(days: int = Query(default=30, ge=1, le=730),
                          tf: str = Query(default=None), _=Depends(require_token)):
    return analytics.full_report(days, tf if tf in ("5m", "15m") else cfg.TIMEFRAME)


@app.get("/api/equity-curve")
async def api_equity_curve(days: int = Query(default=30, ge=1, le=730),
                           tf: str = Query(default=None), _=Depends(require_token)):
    return analytics.equity_curve(days, tf if tf in ("5m", "15m") else cfg.TIMEFRAME)


@app.get("/api/export.csv")
async def api_export(days: int = Query(default=365, ge=1, le=3650),
                     tf: str = Query(default=None), _=Depends(require_token)):
    """The whole ledger, for analysis outside this app."""
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME
    csv_text = analytics.to_csv(days, tf)
    stamp = now_naive().strftime("%Y%m%d")
    return Response(
        content=csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="signals_{tf}_{stamp}.csv"'})


@app.get("/api/universe")
async def api_universe(limit: int = Query(default=200, le=500), _=Depends(require_token)):
    return {"selected": universe.detail(limit), "drop_reasons": universe.last_reason}


@app.get("/api/budget")
async def api_budget(_=Depends(require_token)):
    return budget.check()


@app.get("/api/logs")
async def api_logs(tf: str = Query(default=None), limit: int = Query(default=50, le=300),
                   db: Session = Depends(get_db), _=Depends(require_token)):
    tf = tf if tf in ("5m", "15m") else cfg.TIMEFRAME
    rows = (db.query(ScanLog).filter(ScanLog.timeframe == tf)
              .order_by(ScanLog.timestamp.desc()).limit(limit).all())
    return [{
        "time": r.timestamp.strftime("%H:%M:%S") if r.timestamp else None,
        "symbol": r.symbol, "in_squeeze": r.in_squeeze, "squeeze_bars": r.squeeze_bars,
        "bars_since_fire": r.bars_since_fire, "close": r.close, "adx": r.adx_value,
        "rsi": r.rsi_value, "vol_ratio": r.vol_ratio, "oi_change_pct": r.oi_change_pct,
        "score": r.composite_score, "passed": r.passed, "reason": r.rejection_reason,
    } for r in rows]
