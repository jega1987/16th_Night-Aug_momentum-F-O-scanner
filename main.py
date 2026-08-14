"""
Entry point.

Startup deliberately does NOT die when the broker login fails: Railway would
health-check the container, get nothing, and restart it forever. Instead the
app comes up, reports the failure on /health and on the dashboard, and retries
the connection in the background.

Every schedule below is IST. Railway containers run on UTC, so the scheduler is
pinned to Asia/Kolkata explicitly.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from clock import IST, MarketClock, now_naive
from config import cfg
from database import init_db
from runtime import (budget, engine, feed, notifier, options, scanner,
                     state, store, universe)

logger = logging.getLogger("main")
scheduler = AsyncIOScheduler(timezone=IST)


# --------------------------------------------------------------------------- #
async def connect_feed() -> bool:
    if state.feed_connected:
        return True
    try:
        await feed.connect()
        state.feed_connected = True
        state.feed_error = None
        logger.info("[Startup] %s feed connected", feed.name)
        return True
    except Exception as exc:
        state.feed_connected = False
        state.feed_error = str(exc)
        logger.error("[Startup] Feed connection failed: %s", exc)
        return False


async def job_scan():
    if not state.feed_connected and not await connect_feed():
        return
    try:
        created = await scanner.run_cycle()
        state.last_scan_at = now_naive()
        state.scan_count += 1
        state.last_scan_error = None
        if created:
            logger.info("[Scan] %d new signal(s)", len(created))
    except Exception as exc:
        state.last_scan_error = str(exc)
        logger.exception("[Scan] Cycle failed")
        await notifier.send_error("Scan cycle", str(exc))


async def job_manage_positions():
    if not state.feed_connected or not MarketClock.is_market_open():
        return
    try:
        await engine.manage_open_positions(feed)
    except Exception as exc:
        logger.exception("[Manage] Position update failed: %s", exc)


async def job_snapshots():
    if not state.feed_connected or not MarketClock.is_market_open():
        return
    try:
        await scanner.get_index_snapshots()
    except Exception as exc:
        logger.warning("[Snapshot] %s", exc)


async def job_square_off():
    if not state.feed_connected or not MarketClock.is_market_day():
        return
    closed = await engine.force_square_off(feed, reason="15:20 square-off")
    if closed:
        await notifier.send(f"Squared off {len(closed)} position(s) at the bell.")


async def job_refresh_scrips():
    """Futures codes roll on expiry. Refresh before the open, every trading day."""
    if not state.feed_connected or feed.name != "5paisa":
        return
    try:
        from scrip_resolver import ScripResolver
        resolver = ScripResolver(feed.client)
        force = resolver.any_expiry_today() or now_naive().weekday() in (0, 3)
        resolved = await resolver.refresh_all(force=force)
        feed.INDEX_FUTURES_SCRIPS.update(resolved)
        logger.info("[Scrips] %d contract(s) current (force=%s)", len(resolved), force)
    except Exception as exc:
        logger.error("[Scrips] Refresh failed: %s", exc)
        await notifier.send_error("Scrip refresh", str(exc))


async def job_sample_iv():
    """
    IV rank is only meaningful with history behind it, and history only builds
    if we sample on quiet days too - not just when a signal fires.
    """
    if not state.feed_connected or not cfg.OPTIONS_ENABLED or not MarketClock.is_market_open():
        return
    try:
        await options.record_atm_iv_for_universe()
    except Exception as exc:
        logger.warning("[IV] Sampling failed: %s", exc)


async def job_build_universe():
    """
    Rebuild the tradable stock list before the open. The F&O list and its
    liquidity both move, so this is derived daily rather than hardcoded.
    """
    if not cfg.EQUITY_ENABLED:
        return
    if not state.feed_connected and not await connect_feed():
        return
    try:
        symbols = await universe.build(force=True)
        state.universe_size = len(symbols)
        logger.info("[Universe] %d stocks selected for today", len(symbols))
    except Exception as exc:
        logger.error("[Universe] Build failed: %s", exc)
        await notifier.send_error("Universe build", str(exc))


async def job_premarket_backfill():
    """
    Load history before the open so the first scan of the day isn't competing
    with live trading for the rate limit.
    """
    if not state.feed_connected:
        await connect_feed()
    if not state.feed_connected:
        return
    try:
        await store.backfill(cfg.INDICES, cfg.TIMEFRAME, cfg.HISTORY_BARS)
        store.prune(cfg.CANDLE_RETENTION_DAYS)
        logger.info("[Backfill] %s", store.stats())
    except Exception as exc:
        logger.error("[Backfill] Failed: %s", exc)


async def job_reconnect():
    if not state.feed_connected:
        await connect_feed()


# --------------------------------------------------------------------------- #
def register_jobs():
    # Scans fire 20s after each 5-minute boundary so the candle has closed and
    # the broker has published it.
    scheduler.add_job(job_scan, CronTrigger(minute="*/5", second=20, timezone=IST),
                      id="scan", replace_existing=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(job_manage_positions, "interval", minutes=1,
                      id="manage", replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_snapshots, "interval", minutes=1,
                      id="snapshots", replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_square_off, CronTrigger(day_of_week="mon-fri", hour=15, minute=20, timezone=IST),
                      id="square_off", replace_existing=True)
    scheduler.add_job(job_refresh_scrips, CronTrigger(day_of_week="mon-fri", hour=8, minute=15, timezone=IST),
                      id="scrips", replace_existing=True)
    scheduler.add_job(job_build_universe,
                      CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=IST),
                      id="universe", replace_existing=True)
    scheduler.add_job(job_premarket_backfill,
                      CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone=IST),
                      id="backfill", replace_existing=True)
    scheduler.add_job(job_sample_iv, "interval", minutes=cfg.IV_SAMPLE_MINUTES,
                      id="iv_sample", replace_existing=True, max_instances=1)
    scheduler.add_job(job_reconnect, "interval", minutes=5,
                      id="reconnect", replace_existing=True, max_instances=1)


@asynccontextmanager
async def lifespan(_app):
    state.started_at = now_naive()
    init_db()
    await connect_feed()
    if cfg.EQUITY_ENABLED and state.feed_connected and not universe.load():
        await job_build_universe()      # cold start - don't wait for tomorrow
    if cfg.RUN_SCHEDULER:
        register_jobs()
        scheduler.start()
    else:
        logger.info("[Startup] RUN_SCHEDULER=false - web only, a worker owns the jobs")
    logger.info("[Startup] %s | feed=%s | timeframe=%s | market=%s",
                "Index Squeeze Scanner", cfg.FEED_MODE, cfg.TIMEFRAME, MarketClock.session_label())
    if cfg.FEED_MODE == "mock":
        logger.warning("[Startup] FEED_MODE=mock - every number on the dashboard is synthetic")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("[Shutdown] Scheduler stopped")


# dashboard.app is imported after runtime so the singletons exist first.
from dashboard import app  # noqa: E402

app.router.lifespan_context = lifespan


async def run_worker() -> None:
    """
    Headless mode: scheduler only, no HTTP server.

    Railway can run this as a second service pointed at the same database, so
    the scanning process carries no web-rendering overhead and a dashboard
    request can never slow a scan. Both processes are safe to run together -
    set RUN_SCHEDULER=false on the web service so jobs fire in exactly one.
    """
    state.started_at = now_naive()
    init_db()
    await connect_feed()
    if cfg.EQUITY_ENABLED and state.feed_connected and not universe.load():
        await job_build_universe()
    register_jobs()
    scheduler.start()
    logger.info("[Worker] Scanning only - no dashboard. Cap %d signals/day",
                cfg.MAX_SIGNALS_PER_DAY)

    # A minimal health server, for two reasons. Railway's healthcheck is
    # configured once in railway.json and applies to every service, so a worker
    # with no HTTP listener fails its check and restart-loops forever. And it
    # makes the scanning process observable - whether it is alive, when it last
    # scanned, how much signal budget is left - without reading logs.
    from fastapi import FastAPI

    health_app = FastAPI(title="Scanner worker", docs_url=None, redoc_url=None)

    @health_app.get("/health")
    async def worker_health():
        return {
            "status": "ok",
            "role": "worker",
            "feed": {"mode": cfg.FEED_MODE, "connected": state.feed_connected,
                     "error": state.feed_error},
            "market": MarketClock.session_label(),
            "scheduler_running": scheduler.running,
            "jobs": [j.id for j in scheduler.get_jobs()],
            "scans": state.scan_count,
            "last_scan_at": state.last_scan_at.isoformat() if state.last_scan_at else None,
            "last_scan_error": state.last_scan_error,
            "budget": budget.check(),
            "universe": state.universe_size,
            "stream": getattr(getattr(feed, "stream", None), "status", lambda: None)(),
            "server_time_ist": now_naive().isoformat(timespec="seconds"),
        }

    server = uvicorn.Server(uvicorn.Config(
        health_app, host="0.0.0.0", port=cfg.PORT, log_level="warning"))
    try:
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        logger.info("[Worker] Stopped")


if __name__ == "__main__":
    import sys
    if "--worker" in sys.argv or os.getenv("RUN_MODE", "").lower() == "worker":
        asyncio.run(run_worker())
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=cfg.PORT, log_level=cfg.LOG_LEVEL.lower())
